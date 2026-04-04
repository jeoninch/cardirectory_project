import discord
from discord.ext import commands, tasks
import anthropic
import json
import os
import requests
import base64
import asyncio
from datetime import datetime

# ========================
# 환경변수 설정
# ========================
DISCORD_BOT_TOKEN   = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_CHANNEL_ID  = int(os.environ["DISCORD_CHANNEL_ID"])
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
MY_GITHUB_TOKEN     = os.environ["MY_GITHUB_TOKEN"]
MY_GITHUB_REPO      = os.environ["MY_GITHUB_REPO"]  # "유저명/cardirectory"

# ========================
# 상태 관리
# ========================
# 현재 승인 대기 중인 브랜드 목록
pending_brands = []
# 이미지 업로드 대기 중인 브랜드 (브랜드명: 브랜드 데이터)
waiting_for_image = {}

# ========================
# 봇 초기화
# ========================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)

# ========================
# GitHub API 함수들
# ========================

def github_headers():
    return {
        "Authorization": f"token {MY_GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }

def get_brands_json():
    """GitHub에서 brands.json 읽기"""
    url = f"https://api.github.com/repos/{MY_GITHUB_REPO}/contents/brands.json"
    res = requests.get(url, headers=github_headers())
    data = res.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    return json.loads(content), data["sha"]

def update_brands_json(brands, sha):
    """GitHub brands.json 업데이트"""
    url = f"https://api.github.com/repos/{MY_GITHUB_REPO}/contents/brands.json"
    content = base64.b64encode(
        json.dumps(brands, ensure_ascii=False, indent=2).encode("utf-8")
    ).decode("utf-8")
    payload = {
        "message": "✅ 새 브랜드 자동 추가",
        "content": content,
        "sha": sha
    }
    res = requests.put(url, headers=github_headers(), json=payload)
    return res.status_code == 200

def upload_banner_to_github(image_bytes, filename):
    """배너 이미지를 GitHub banners/ 폴더에 업로드"""
    url = f"https://api.github.com/repos/{MY_GITHUB_REPO}/contents/banners/{filename}"
    
    # 이미 파일이 있으면 sha 가져오기
    sha = None
    res = requests.get(url, headers=github_headers())
    if res.status_code == 200:
        sha = res.json()["sha"]
    
    content = base64.b64encode(image_bytes).decode("utf-8")
    payload = {
        "message": f"🖼️ 배너 추가: {filename}",
        "content": content,
    }
    if sha:
        payload["sha"] = sha
    
    res = requests.put(url, headers=github_headers(), json=payload)
    return res.status_code in [200, 201]

# ========================
# Claude 브랜드 탐색
# ========================

def search_new_brands(existing_hrefs):
    """Claude API로 새 브랜드 탐색"""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    existing_list = ", ".join(existing_hrefs)

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1000,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{
            "role": "user",
            "content": f"""
            카디스트리 및 플레잉 카드 관련 브랜드를 웹에서 검색해줘.
            아래 URL 목록에 없는 새로운 브랜드만 찾아줘:
            기존 목록: {existing_list}

            결과는 반드시 JSON 형태로만 답해줘 (다른 말 없이):
            [
              {{"name": "브랜드명", "href": "공식URL"}}
            ]
            새로운 브랜드가 없으면 빈 배열 [] 반환.
            """
        }]
    )

    text = "".join(b.text for b in response.content if hasattr(b, "text"))
    try:
        start = text.find("[")
        end = text.rfind("]") + 1
        return json.loads(text[start:end])
    except:
        return []

# ========================
# 주간 자동 탐색 (매주 월요일)
# ========================

async def run_brand_check(channel):
    """브랜드 탐색 실제 로직 (check 명령어와 스케줄러 공용)"""
    await channel.send("🔍 **새 브랜드 탐색 시작...**")

    try:
        brands, _ = get_brands_json()
        existing_hrefs = [b["href"].lower() for b in brands]
        new_brands = search_new_brands(existing_hrefs)

        if not new_brands:
            await channel.send("✅ 이번 주는 새로운 브랜드가 없어요!")
            return

        global pending_brands
        pending_brands = new_brands

        msg = f"🆕 **{len(new_brands)}개의 새 브랜드 발견!**\n\n"
        for i, b in enumerate(new_brands, 1):
            msg += f"`{i}.` **{b['name']}**\n"
            msg += f"    {b['href']}\n\n"
        msg += "━━━━━━━━━━━━━━━━━━━━\n"
        msg += "추가할 브랜드 번호로 배너 업로드를 시작하세요!\n"
        msg += "예: `/add 1` → 배너 이미지 첨부해서 전송\n"
        msg += "`/skipall` → 전부 건너뛰기"

        await channel.send(msg)

    except Exception as e:
        await channel.send(f"❌ 탐색 중 오류 발생: {e}")

@tasks.loop(hours=168)  # 7일 = 168시간
async def weekly_brand_check():
    channel = bot.get_channel(DISCORD_CHANNEL_ID)
    await run_brand_check(channel)

# ========================
# 디스코드 명령어
# ========================

@bot.event
async def on_ready():
    print(f"✅ 봇 시작됨: {bot.user}")
    weekly_brand_check.start()

@bot.command()
async def add(ctx, brand_num: int):
    """특정 번호의 브랜드 배너 업로드 시작"""
    global pending_brands, waiting_for_image

    if not pending_brands:
        await ctx.send("⚠️ 대기 중인 브랜드가 없어요. `/check` 로 먼저 탐색해보세요!")
        return

    if brand_num < 1 or brand_num > len(pending_brands):
        await ctx.send(f"⚠️ 1~{len(pending_brands)} 사이의 번호를 입력해주세요.")
        return

    brand = pending_brands[brand_num - 1]
    waiting_for_image[ctx.author.id] = brand

    await ctx.send(
        f"📎 **{brand['name']}** 배너 이미지를 이 채널에 첨부해서 보내주세요!\n"
        f"지원 형식: `.webp` `.png` `.jpg`\n"
        f"취소하려면 `/cancel` 입력"
    )

@bot.command()
async def check(ctx):
    """수동으로 새 브랜드 탐색 실행"""
    await run_brand_check(ctx.channel)

@bot.command()
async def cancel(ctx):
    """이미지 업로드 대기 취소"""
    if ctx.author.id in waiting_for_image:
        brand = waiting_for_image.pop(ctx.author.id)
        await ctx.send(f"❌ **{brand['name']}** 배너 업로드가 취소되었습니다.")
    else:
        await ctx.send("⚠️ 취소할 작업이 없어요.")

@bot.command()
async def skipall(ctx):
    """대기 중인 브랜드 전부 건너뛰기"""
    global pending_brands
    pending_brands = []
    await ctx.send("⏭️ 이번 주 탐색 결과를 모두 건너뛰었습니다.")

@bot.command()
async def status(ctx):
    """현재 대기 중인 브랜드 목록 확인"""
    if not pending_brands:
        await ctx.send("📭 대기 중인 브랜드가 없어요.")
        return

    msg = f"📋 **대기 중인 브랜드 ({len(pending_brands)}개)**\n\n"
    for i, b in enumerate(pending_brands, 1):
        msg += f"`{i}.` **{b['name']}** - {b['href']}\n"
    await ctx.send(msg)

# ========================
# 이미지 첨부 감지
# ========================

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # 이미지 대기 중인 유저가 이미지를 보냈을 때
    if message.author.id in waiting_for_image and message.attachments:
        attachment = message.attachments[0]

        # 이미지 파일 확인
        valid_extensions = [".webp", ".png", ".jpg", ".jpeg"]
        if not any(attachment.filename.lower().endswith(ext) for ext in valid_extensions):
            await message.channel.send("⚠️ `.webp`, `.png`, `.jpg` 형식만 지원해요!")
            return

        brand = waiting_for_image.pop(message.author.id)
        processing_msg = await message.channel.send(
            f"⏳ **{brand['name']}** 배너 업로드 중..."
        )

        try:
            # 이미지 다운로드
            image_bytes = requests.get(attachment.url).content

            # 파일명 생성 (브랜드명 기반)
            import re
            filename = re.sub(r'[^a-z0-9]', '', brand['name'].lower())
            ext = os.path.splitext(attachment.filename)[1].lower()
            filename = filename + ext

            # GitHub banners/ 폴더에 업로드
            banner_ok = upload_banner_to_github(image_bytes, filename)
            if not banner_ok:
                await processing_msg.edit(content=f"❌ 배너 업로드 실패!")
                return

            # brands.json 업데이트
            brands, sha = get_brands_json()
            new_entry = {
                "href": brand["href"],
                "imgSrc": f"banners/{filename}"
            }
            brands.append(new_entry)
            json_ok = update_brands_json(brands, sha)

            if json_ok:
                await processing_msg.edit(
                    content=f"✅ **{brand['name']}** 추가 완료!\n"
                            f"🖼️ 배너: `banners/{filename}`\n"
                            f"🔗 {brand['href']}\n"
                            f"🚀 Cloudflare 자동 배포 중..."
                )
                # 완료된 브랜드를 pending에서 제거
                global pending_brands
                pending_brands = [b for b in pending_brands if b["href"] != brand["href"]]

                # 남은 브랜드 안내
                if pending_brands:
                    await message.channel.send(
                        f"📋 아직 **{len(pending_brands)}개** 브랜드가 남아있어요!\n"
                        f"`/status` 로 확인하거나 `/add [번호]` 로 계속 추가하세요."
                    )
                else:
                    await message.channel.send("🎉 모든 브랜드 처리 완료!")
            else:
                await processing_msg.edit(content=f"❌ brands.json 업데이트 실패!")

        except Exception as e:
            await processing_msg.edit(content=f"❌ 오류 발생: {e}")

    # 일반 명령어 처리
    await bot.process_commands(message)

# ========================
# 봇 실행
# ========================
bot.run(DISCORD_BOT_TOKEN)
