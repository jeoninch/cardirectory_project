import discord
from discord.ext import commands, tasks
import anthropic
import json
import os
import requests
import base64
import re
from datetime import datetime

# ========================
# 환경변수
# ========================
DISCORD_BOT_TOKEN  = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_CHANNEL_ID = int(os.environ["DISCORD_CHANNEL_ID"])
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
MY_GITHUB_TOKEN    = os.environ["MY_GITHUB_TOKEN"]
MY_GITHUB_REPO     = os.environ["MY_GITHUB_REPO"]  # "유저명/cardirectory"

# ========================
# 카테고리 설정
# ========================
# needs_image: True면 배너 이미지 필요, False면 이미지 없이 바로 추가
CATEGORIES = {
    "brands": {
        "file": "brands.json",
        "needs_image": True,
        "prompt": "카디스트리 관련 브랜드 (덱 제작, 카디스트리 아티스트 브랜드 등)",
        "emoji": "🃏"
    },
    "wholesale": {
        "file": "wholesale.json",
        "needs_image": True,
        "prompt": "플레잉 카드 마켓, 도매, 쇼핑몰 (카드를 판매하는 스토어)",
        "emoji": "🛍️"
    },
    "events": {
        "file": "events.json",
        "needs_image": False,
        "prompt": "카디스트리 이벤트, 컨벤션, 대회",
        "emoji": "🎪"
    },
    "community": {
        "file": "community.json",
        "needs_image": False,
        "prompt": "카디스트리 커뮤니티 (디스코드, 인스타그램 그룹, 레딧 등)",
        "emoji": "👥"
    },
    "blogs": {
        "file": "blogs.json",
        "needs_image": False,
        "prompt": "카디스트리 관련 블로그, 뉴스레터, 미디어",
        "emoji": "📝"
    }
}

# ========================
# 상태 관리
# ========================
pending_by_category = {cat: [] for cat in CATEGORIES}  # 카테고리별 대기 목록
waiting_for_image = {}  # 이미지 대기 중인 유저 {user_id: {brand 데이터 + category}}

# ========================
# 봇 초기화
# ========================
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)

# ========================
# GitHub API
# ========================

def github_headers():
    return {
        "Authorization": f"token {MY_GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }

def get_json_from_github(filename):
    """GitHub에서 JSON 파일 읽기"""
    url = f"https://api.github.com/repos/{MY_GITHUB_REPO}/contents/{filename}"
    res = requests.get(url, headers=github_headers())
    data = res.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    return json.loads(content), data["sha"]

def update_json_on_github(filename, data, sha, commit_msg):
    """GitHub JSON 파일 업데이트"""
    url = f"https://api.github.com/repos/{MY_GITHUB_REPO}/contents/{filename}"
    content = base64.b64encode(
        json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    ).decode("utf-8")
    payload = {"message": commit_msg, "content": content, "sha": sha}
    res = requests.put(url, headers=github_headers(), json=payload)
    return res.status_code == 200

def upload_banner_to_github(image_bytes, filename):
    """배너 이미지를 GitHub banners/ 폴더에 업로드"""
    url = f"https://api.github.com/repos/{MY_GITHUB_REPO}/contents/banners/{filename}"
    sha = None
    res = requests.get(url, headers=github_headers())
    if res.status_code == 200:
        sha = res.json()["sha"]
    content = base64.b64encode(image_bytes).decode("utf-8")
    payload = {"message": f"🖼️ 배너 추가: {filename}", "content": content}
    if sha:
        payload["sha"] = sha
    res = requests.put(url, headers=github_headers(), json=payload)
    return res.status_code in [200, 201]

def get_all_existing_hrefs():
    """모든 카테고리 JSON에서 기존 href 목록 수집 (중복 방지)"""
    all_hrefs = set()
    for cat, info in CATEGORIES.items():
        try:
            data, _ = get_json_from_github(info["file"])
            for item in data:
                all_hrefs.add(item["href"].lower().rstrip("/"))
        except:
            pass
    return all_hrefs

# ========================
# Claude 브랜드 탐색
# ========================

def search_new_items(category, existing_hrefs):
    """Claude API로 특정 카테고리 새 항목 탐색"""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    cat_info = CATEGORIES[category]
    existing_list = ", ".join(existing_hrefs) if existing_hrefs else "없음"

    # 카테고리별 필드 다르게 요청
    if category in ["brands", "wholesale"]:
        json_format = '[{"href": "공식URL"}]'
    else:
        json_format = '[{"name": "이름", "href": "공식URL"}]'

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1000,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{
            "role": "user",
            "content": f"""
            다음 카테고리에 해당하는 새로운 항목을 웹에서 검색해줘:
            카테고리: {cat_info['prompt']}

            아래 URL 목록에 없는 새로운 항목만 찾아줘:
            기존 목록: {existing_list}

            결과는 반드시 JSON 형태로만 답해줘 (다른 말 없이):
            {json_format}

            새로운 항목이 없으면 빈 배열 [] 반환.
            최대 5개까지만 반환해줘.
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
# 탐색 실행 함수
# ========================

async def run_brand_check(channel):
    """전체 카테고리 탐색 실행"""
    await channel.send("🔍 **전체 카테고리 탐색 시작...**")

    try:
        # 모든 카테고리의 기존 href 수집 (중복 방지)
        all_existing = get_all_existing_hrefs()
        total_found = 0

        for category, cat_info in CATEGORIES.items():
            await channel.send(f"{cat_info['emoji']} **{category}** 탐색 중...")

            new_items = search_new_items(category, all_existing)

            if not new_items:
                await channel.send(f"  └ 새 항목 없음")
                continue

            pending_by_category[category] = new_items
            total_found += len(new_items)

            msg = f"  └ **{len(new_items)}개 발견!**\n"
            for i, item in enumerate(new_items, 1):
                name = item.get("name", item.get("href", ""))
                msg += f"    `{i}.` {name}\n"
                msg += f"        {item['href']}\n"

            # 이미지 불필요한 카테고리는 바로 추가 안내
            if not cat_info["needs_image"]:
                msg += f"    → `/approve {category}` 로 바로 추가 가능\n"
            else:
                msg += f"    → `/add {category} [번호]` 로 배너 업로드\n"

            await channel.send(msg)

        if total_found == 0:
            await channel.send("✅ 모든 카테고리에서 새 항목이 없어요!")
        else:
            await channel.send(
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"총 **{total_found}개** 발견! 위 안내에 따라 추가해주세요."
            )

    except Exception as e:
        await channel.send(f"❌ 탐색 중 오류 발생: {e}")

# ========================
# 주간 자동 탐색
# ========================

@tasks.loop(hours=168)
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
async def check(ctx):
    """수동으로 전체 카테고리 탐색"""
    await run_brand_check(ctx.channel)

@bot.command()
async def add(ctx, category: str, item_num: int):
    """이미지 필요한 카테고리 배너 업로드 시작 (brands, wholesale)"""
    if category not in CATEGORIES:
        await ctx.send(f"⚠️ 카테고리를 확인해주세요: {', '.join(CATEGORIES.keys())}")
        return

    if not CATEGORIES[category]["needs_image"]:
        await ctx.send(f"⚠️ `{category}` 는 이미지가 없어요! `/approve {category}` 를 사용해주세요.")
        return

    items = pending_by_category.get(category, [])
    if not items:
        await ctx.send(f"⚠️ `{category}` 에 대기 중인 항목이 없어요.")
        return

    if item_num < 1 or item_num > len(items):
        await ctx.send(f"⚠️ 1~{len(items)} 사이의 번호를 입력해주세요.")
        return

    item = items[item_num - 1]
    waiting_for_image[ctx.author.id] = {**item, "category": category}

    await ctx.send(
        f"📎 **{item.get('href')}** 배너 이미지를 첨부해서 보내주세요!\n"
        f"지원 형식: `.webp` `.png` `.jpg`\n"
        f"취소: `/cancel`"
    )

@bot.command()
async def approve(ctx, category: str):
    """이미지 불필요한 카테고리 바로 추가 (events, community, blogs)"""
    if category not in CATEGORIES:
        await ctx.send(f"⚠️ 카테고리를 확인해주세요: {', '.join(CATEGORIES.keys())}")
        return

    if CATEGORIES[category]["needs_image"]:
        await ctx.send(f"⚠️ `{category}` 는 배너 이미지가 필요해요! `/add {category} [번호]` 를 사용해주세요.")
        return

    items = pending_by_category.get(category, [])
    if not items:
        await ctx.send(f"⚠️ `{category}` 에 대기 중인 항목이 없어요.")
        return

    processing_msg = await ctx.send(f"⏳ `{category}` 항목 추가 중...")

    try:
        data, sha = get_json_from_github(CATEGORIES[category]["file"])
        cat_info = CATEGORIES[category]

        for item in items:
            new_entry = {"href": item["href"]}
            if "name" in item:
                new_entry["name"] = item["name"]
            data.append(new_entry)

        ok = update_json_on_github(
            cat_info["file"], data, sha,
            f"✅ {category} 새 항목 추가 ({len(items)}개)"
        )

        if ok:
            added_list = "\n".join([f"  • {i.get('name', i['href'])}" for i in items])
            await processing_msg.edit(
                content=f"✅ **{category}** {len(items)}개 추가 완료!\n{added_list}\n🚀 Cloudflare 배포 중..."
            )
            pending_by_category[category] = []
        else:
            await processing_msg.edit(content=f"❌ `{category}` 업데이트 실패!")

    except Exception as e:
        await processing_msg.edit(content=f"❌ 오류 발생: {e}")

@bot.command()
async def cancel(ctx):
    """이미지 업로드 대기 취소"""
    if ctx.author.id in waiting_for_image:
        item = waiting_for_image.pop(ctx.author.id)
        await ctx.send(f"❌ 배너 업로드가 취소되었습니다.")
    else:
        await ctx.send("⚠️ 취소할 작업이 없어요.")

@bot.command()
async def status(ctx):
    """전체 카테고리 대기 목록 확인"""
    has_any = False
    msg = "📋 **대기 중인 항목**\n\n"
    for category, items in pending_by_category.items():
        if items:
            has_any = True
            cat_info = CATEGORIES[category]
            msg += f"{cat_info['emoji']} **{category}** ({len(items)}개)\n"
            for i, item in enumerate(items, 1):
                name = item.get("name", item["href"])
                msg += f"  `{i}.` {name}\n"
            msg += "\n"
    if not has_any:
        await ctx.send("📭 대기 중인 항목이 없어요.")
    else:
        await ctx.send(msg)

@bot.command()
async def skipall(ctx):
    """전체 대기 목록 초기화"""
    global pending_by_category
    pending_by_category = {cat: [] for cat in CATEGORIES}
    await ctx.send("⏭️ 모든 대기 항목을 초기화했습니다.")

# ========================
# 이미지 첨부 감지
# ========================

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if message.author.id in waiting_for_image and message.attachments:
        attachment = message.attachments[0]
        valid_extensions = [".webp", ".png", ".jpg", ".jpeg"]

        if not any(attachment.filename.lower().endswith(ext) for ext in valid_extensions):
            await message.channel.send("⚠️ `.webp` `.png` `.jpg` 형식만 지원해요!")
            return

        item = waiting_for_image.pop(message.author.id)
        category = item["category"]
        processing_msg = await message.channel.send(f"⏳ 배너 업로드 중...")

        try:
            # 이미지 다운로드
            image_bytes = requests.get(attachment.url).content

            # 파일명 생성
            filename = re.sub(r'[^a-z0-9]', '', item["href"].lower().replace("https://", "").replace("www.", "").split("/")[0])
            ext = os.path.splitext(attachment.filename)[1].lower()
            filename = filename + ext

            # GitHub banners/ 에 업로드
            banner_ok = upload_banner_to_github(image_bytes, filename)
            if not banner_ok:
                await processing_msg.edit(content="❌ 배너 업로드 실패!")
                return

            # JSON 업데이트
            cat_info = CATEGORIES[category]
            data, sha = get_json_from_github(cat_info["file"])
            new_entry = {
                "href": item["href"],
                "imgSrc": f"banners/{filename}"
            }
            data.append(new_entry)
            json_ok = update_json_on_github(
                cat_info["file"], data, sha,
                f"✅ {category} 새 항목 추가: {item['href']}"
            )

            if json_ok:
                await processing_msg.edit(
                    content=f"✅ 추가 완료!\n"
                            f"🖼️ `banners/{filename}`\n"
                            f"🔗 {item['href']}\n"
                            f"🚀 Cloudflare 배포 중..."
                )
                # pending에서 제거
                pending_by_category[category] = [
                    b for b in pending_by_category[category]
                    if b["href"] != item["href"]
                ]

                remaining = len(pending_by_category[category])
                if remaining > 0:
                    await message.channel.send(f"📋 `{category}` 에 아직 **{remaining}개** 남아있어요!")
                else:
                    await message.channel.send(f"🎉 `{category}` 모두 처리 완료!")
            else:
                await processing_msg.edit(content="❌ JSON 업데이트 실패!")

        except Exception as e:
            await processing_msg.edit(content=f"❌ 오류 발생: {e}")

    await bot.process_commands(message)

# ========================
# 봇 실행
# ========================
bot.run(DISCORD_BOT_TOKEN)
