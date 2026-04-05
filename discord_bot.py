import discord
from discord.ext import commands, tasks
import anthropic
import json
import os
import requests
import base64
import re

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
CATEGORIES = {
    "brands": {
        "file": "brands.json",
        "needs_image": True,
        "prompt": "cardistry brand (deck makers, cardistry artist brands)",
        "emoji": "🃏"
    },
    "wholesale": {
        "file": "wholesale.json",
        "needs_image": True,
        "prompt": "playing card market or store (shops that sell playing cards)",
        "emoji": "🛍️"
    },
    "events": {
        "file": "events.json",
        "needs_image": False,
        "prompt": "cardistry event, convention, or competition",
        "emoji": "🎪"
    },
    "community": {
        "file": "community.json",
        "needs_image": False,
        "prompt": "cardistry community (discord server, instagram group, reddit etc)",
        "emoji": "👥"
    },
    "blogs": {
        "file": "blogs.json",
        "needs_image": False,
        "prompt": "cardistry blog, newsletter, or media",
        "emoji": "📝"
    }
}

# ========================
# 상태 관리
# ========================
pending_by_category = {cat: [] for cat in CATEGORIES}
waiting_for_image = {}

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
    url = f"https://api.github.com/repos/{MY_GITHUB_REPO}/contents/{filename}"
    res = requests.get(url, headers=github_headers())
    data = res.json()
    content = base64.b64decode(data["content"]).decode("utf-8")
    return json.loads(content), data["sha"]

def update_json_on_github(filename, data, sha, commit_msg):
    url = f"https://api.github.com/repos/{MY_GITHUB_REPO}/contents/{filename}"
    content = base64.b64encode(
        json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
    ).decode("utf-8")
    payload = {"message": commit_msg, "content": content, "sha": sha}
    res = requests.put(url, headers=github_headers(), json=payload)
    return res.status_code == 200

def upload_banner_to_github(image_bytes, filename):
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

def get_all_existing_domains():
    """
    비용 절감: URL 전체 대신 도메인만 추출해서 전송
    예) https://www.fontainecards.com → fontainecards.com
    """
    all_domains = set()
    for cat, info in CATEGORIES.items():
        try:
            data, _ = get_json_from_github(info["file"])
            for item in data:
                href = item["href"].lower()
                # 도메인만 추출
                domain = re.sub(r'https?://(www\.)?', '', href).split("/")[0]
                all_domains.add(domain)
        except:
            pass
    return all_domains

# ========================
# Claude 탐색 (비용 최적화 - 단일 호출)
# ========================

def search_all_categories(existing_domains):
    """
    💰 비용 절감 포인트:
    - 카테고리 5개를 API 1번 호출로 한꺼번에 탐색
    - claude-haiku 사용 (가장 저렴)
    - 도메인만 전달 (URL 전체 X)
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    domain_list = ", ".join(sorted(existing_domains)[:80]) if existing_domains else "none"

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=800,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{
            "role": "user",
            "content": (
                "Search for NEW cardistry-related websites NOT in this domain list:\n"
                f"{domain_list}\n\n"
                "Find up to 2 new items per category and reply ONLY with this JSON format:\n"
                "{\n"
                '  "brands": [{"href": "URL"}],\n'
                '  "wholesale": [{"href": "URL"}],\n'
                '  "events": [{"name": "name", "href": "URL"}],\n'
                '  "community": [{"name": "name", "href": "URL"}],\n'
                '  "blogs": [{"name": "name", "href": "URL"}]\n'
                "}\n"
                "Use [] for categories with nothing new."
            )
        }]
    )

    text = "".join(b.text for b in response.content if hasattr(b, "text"))
    try:
        start = text.find("{")
        end = text.rfind("}") + 1
        results = json.loads(text[start:end])

        # 중복 도메인 필터링
        filtered = {}
        for category, items in results.items():
            if category not in CATEGORIES:
                continue
            clean = []
            for item in items:
                domain = re.sub(r'https?://(www\.)?', '', item["href"].lower()).split("/")[0]
                if domain not in existing_domains:
                    clean.append(item)
            filtered[category] = clean
        return filtered
    except:
        return {cat: [] for cat in CATEGORIES}

# ========================
# 탐색 실행 함수
# ========================

async def run_brand_check(channel):
    await channel.send("🔍 **전체 카테고리 탐색 중... (API 1회 호출)**")

    try:
        # 모든 기존 도메인 수집
        all_existing = get_all_existing_domains()

        # 💰 API 1번만 호출해서 전체 카테고리 탐색
        results = search_all_categories(all_existing)

        total_found = 0
        for category, new_items in results.items():
            cat_info = CATEGORIES[category]

            if not new_items:
                continue

            pending_by_category[category] = new_items
            total_found += len(new_items)

            msg = f"{cat_info['emoji']} **{category}** {len(new_items)}개 발견!\n"
            for i, item in enumerate(new_items, 1):
                name = item.get("name", item.get("href", ""))
                msg += f"  `{i}.` {name}\n"
                msg += f"      {item['href']}\n"

            if not cat_info["needs_image"]:
                msg += f"  → `/approve {category}` 로 바로 추가\n"
            else:
                msg += f"  → `/add {category} [번호]` 후 배너 첨부\n"

            await channel.send(msg)

        if total_found == 0:
            await channel.send("✅ 모든 카테고리 새 항목 없음!")
        else:
            await channel.send(f"━━━━━━━━━━━━━━━━━━━━\n총 **{total_found}개** 발견!")

    except Exception as e:
        await channel.send(f"❌ 탐색 오류: {e}")

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
    """brands/wholesale 배너 업로드 시작"""
    if category not in CATEGORIES:
        await ctx.send(f"⚠️ 카테고리: {', '.join(CATEGORIES.keys())}")
        return
    if not CATEGORIES[category]["needs_image"]:
        await ctx.send(f"⚠️ `{category}` 는 `/approve {category}` 를 사용해주세요.")
        return

    items = pending_by_category.get(category, [])
    if not items:
        await ctx.send(f"⚠️ `{category}` 대기 항목 없음. `/check` 먼저 실행해주세요.")
        return
    if item_num < 1 or item_num > len(items):
        await ctx.send(f"⚠️ 1~{len(items)} 사이 번호를 입력해주세요.")
        return

    item = items[item_num - 1]
    waiting_for_image[ctx.author.id] = {**item, "category": category}
    await ctx.send(
        f"📎 **{item.get('href')}** 배너를 첨부해서 보내주세요!\n"
        f"형식: `.webp` `.png` `.jpg` | 취소: `/cancel`"
    )

@bot.command()
async def approve(ctx, category: str):
    """events/community/blogs 이미지 없이 바로 추가"""
    if category not in CATEGORIES:
        await ctx.send(f"⚠️ 카테고리: {', '.join(CATEGORIES.keys())}")
        return
    if CATEGORIES[category]["needs_image"]:
        await ctx.send(f"⚠️ `{category}` 는 `/add {category} [번호]` 를 사용해주세요.")
        return

    items = pending_by_category.get(category, [])
    if not items:
        await ctx.send(f"⚠️ `{category}` 대기 항목 없음.")
        return

    processing_msg = await ctx.send(f"⏳ `{category}` 추가 중...")
    try:
        data, sha = get_json_from_github(CATEGORIES[category]["file"])
        for item in items:
            new_entry = {"href": item["href"]}
            if "name" in item:
                new_entry["name"] = item["name"]
            data.append(new_entry)

        ok = update_json_on_github(
            CATEGORIES[category]["file"], data, sha,
            f"✅ {category} {len(items)}개 추가"
        )
        if ok:
            added = "\n".join([f"  • {i.get('name', i['href'])}" for i in items])
            await processing_msg.edit(
                content=f"✅ **{category}** {len(items)}개 추가 완료!\n{added}\n🚀 배포 중..."
            )
            pending_by_category[category] = []
        else:
            await processing_msg.edit(content=f"❌ `{category}` 업데이트 실패!")
    except Exception as e:
        await processing_msg.edit(content=f"❌ 오류: {e}")

@bot.command()
async def cancel(ctx):
    """이미지 업로드 취소"""
    if ctx.author.id in waiting_for_image:
        waiting_for_image.pop(ctx.author.id)
        await ctx.send("❌ 배너 업로드 취소됨.")
    else:
        await ctx.send("⚠️ 취소할 작업 없음.")

@bot.command()
async def status(ctx):
    """대기 목록 확인"""
    has_any = False
    msg = "📋 **대기 중인 항목**\n\n"
    for category, items in pending_by_category.items():
        if items:
            has_any = True
            cat_info = CATEGORIES[category]
            msg += f"{cat_info['emoji']} **{category}** ({len(items)}개)\n"
            for i, item in enumerate(items, 1):
                msg += f"  `{i}.` {item.get('name', item['href'])}\n"
            msg += "\n"
    await ctx.send("📭 대기 항목 없음." if not has_any else msg)

@bot.command()
async def skipall(ctx):
    """전체 대기 목록 초기화"""
    global pending_by_category
    pending_by_category = {cat: [] for cat in CATEGORIES}
    await ctx.send("⏭️ 전체 초기화 완료.")

# ========================
# 이미지 첨부 감지
# ========================

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if message.author.id in waiting_for_image and message.attachments:
        attachment = message.attachments[0]
        valid_ext = [".webp", ".png", ".jpg", ".jpeg"]

        if not any(attachment.filename.lower().endswith(e) for e in valid_ext):
            await message.channel.send("⚠️ `.webp` `.png` `.jpg` 형식만 지원해요!")
            return

        item = waiting_for_image.pop(message.author.id)
        category = item["category"]
        processing_msg = await message.channel.send("⏳ 배너 업로드 중...")

        try:
            image_bytes = requests.get(attachment.url).content

            # 파일명: 도메인 기반으로 생성
            domain = re.sub(r'https?://(www\.)?', '', item["href"].lower()).split("/")[0]
            filename = re.sub(r'[^a-z0-9]', '', domain)
            ext = os.path.splitext(attachment.filename)[1].lower()
            filename = filename + ext

            banner_ok = upload_banner_to_github(image_bytes, filename)
            if not banner_ok:
                await processing_msg.edit(content="❌ 배너 업로드 실패!")
                return

            data, sha = get_json_from_github(CATEGORIES[category]["file"])
            data.append({"href": item["href"], "imgSrc": f"banners/{filename}"})
            json_ok = update_json_on_github(
                CATEGORIES[category]["file"], data, sha,
                f"✅ {category} 추가: {item['href']}"
            )

            if json_ok:
                await processing_msg.edit(
                    content=f"✅ 추가 완료!\n🖼️ `banners/{filename}`\n🔗 {item['href']}\n🚀 배포 중..."
                )
                pending_by_category[category] = [
                    b for b in pending_by_category[category]
                    if b["href"] != item["href"]
                ]
                remaining = len(pending_by_category[category])
                if remaining > 0:
                    await message.channel.send(f"📋 `{category}` 아직 **{remaining}개** 남음!")
                else:
                    await message.channel.send(f"🎉 `{category}` 모두 완료!")
            else:
                await processing_msg.edit(content="❌ JSON 업데이트 실패!")

        except Exception as e:
            await processing_msg.edit(content=f"❌ 오류: {e}")

    await bot.process_commands(message)

# ========================
# 봇 실행
# ========================
bot.run(DISCORD_BOT_TOKEN)
