import json
import subprocess

def approve():
    # pending 브랜드를 brands.json에 추가
    with open("pending_brands.json", "r", encoding="utf-8") as f:
        pending = json.load(f)

    if not pending:
        print("추가할 브랜드가 없습니다.")
        return

    with open("brands.json", "r", encoding="utf-8") as f:
        brands = json.load(f)

    brands.extend(pending)

    with open("brands.json", "w", encoding="utf-8") as f:
        json.dump(brands, f, ensure_ascii=False, indent=2)

    # pending 파일 비우기
    with open("pending_brands.json", "w") as f:
        json.dump([], f)

    print(f"✅ {len(pending)}개 브랜드 추가 완료")

if __name__ == "__main__":
    approve()
