import anthropic
import json
import os
import discord
from discord.ext import commands
import asyncio

DISCORD_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_CHANNEL_ID = int(os.environ["DISCORD_CHANNEL_ID"])

# 기존 브랜드 목록 불러오기
def load_existing_brands():
    with open("./data/brands.json", "r", encoding="utf-8") as f:
        brands = json.load(f)
    return [b["href"].lower() for b in brands]

# Claude로 새 브랜드 탐색
def search_new_brands(existing_brands):
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    existing_list = ", ".join(existing_brands)

    response = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=1000,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{
            "role": "user",
            "content": f"""
            카디스트리 및 플레잉 카드 관련 브랜드를 웹에서 검색해줘.
            아래 목록에 없는 새로운 브랜드만 찾아줘:
            기존 브랜드 URL 목록: {existing_list}

            결과는 JSON 형태로만 답해줘:
            [
              {{"name": "브랜드명", "href": "공식URL", "imgSrc": "banners/파일명.webp"}}
            ]
            없으면 빈 배열 [] 반환.
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

# 디스코드로 전송
async def send_discord(new_brands):
    intents = discord.Intents.default()
    intents.message_content = True
    bot = commands.Bot(command_prefix="/", intents=intents)

    @bot.event
    async def on_ready():
        channel = bot.get_channel(DISCORD_CHANNEL_ID)

        if not new_brands:
            await channel.send("✅ 새로운 브랜드가 발견되지 않았습니다.")
            await bot.close()
            return

        # pending 저장
        with open("./data/pending_brands.json", "w", encoding="utf-8") as f:
            json.dump(new_brands, f, ensure_ascii=False)

        # 메시지 구성
        message = "🆕 **새로운 카디스트리 브랜드 발견!**\n\n"
        for b in new_brands:
            message += f"• **{b['name']}**\n"
            message += f"  {b['href']}\n\n"
        message += "추가하려면 `/approve`, 취소하려면 `/reject` 입력"

        await channel.send(message)
        await bot.close()

    @bot.command()
    async def approve(ctx):
        import requests
        headers = {
            "Authorization": f"token {os.environ['MY_GITHUB_TOKEN']}",
            "Accept": "application/vnd.github.v3+json"
        }
        repo = os.environ["MY_GITHUB_REPO"]  # 예: "유저명/cardirectory"
        requests.post(
            f"https://api.github.com/repos/{repo}/actions/workflows/approve-brands.yml/dispatches",
            headers=headers,
            json={"ref": "main"}
        )
        await ctx.send("✅ 승인 완료! 잠시 후 사이트에 반영됩니다.")

    @bot.command()
    async def reject(ctx):
        with open("./data/pending_brands.json", "w") as f:
            json.dump([], f)
        await ctx.send("❌ 거절되었습니다.")

    await bot.start(DISCORD_TOKEN)

if __name__ == "__main__":
    existing = load_existing_brands()
    new_brands = search_new_brands(existing)
    asyncio.run(send_discord(new_brands))
