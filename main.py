import os
import csv
import io
import json
import re
import time
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse

BOT_TOKEN = os.getenv("BOT_TOKEN")
SHEET_CSV_URL = os.getenv("SHEET_CSV_URL")


def clean_text(text):
    return re.sub(r"\s+", " ", text or "").strip()


def remove_domain_links(text, domain):
    text = text or ""

    if not domain:
        return text.strip()

    domain = domain.replace("https://", "").replace("http://", "").strip("/")
    escaped = re.escape(domain)

    patterns = [
        rf"https?://www\.{escaped}\S*",
        rf"https?://{escaped}\S*",
        rf"www\.{escaped}\S*",
        rf"{escaped}\S*",
    ]

    for pattern in patterns:
        text = re.sub(pattern, "", text, flags=re.IGNORECASE)

    text = re.sub(r"\s+", " ", text)
    return text.strip()


def load_seen(seen_file):
    if not os.path.exists(seen_file):
        return set()

    try:
        with open(seen_file, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(seen_file, seen):
    with open(seen_file, "w", encoding="utf-8") as f:
        json.dump(list(seen)[-500:], f, ensure_ascii=False, indent=2)


def get_html(url):
    headers = {
        "User-Agent": "Mozilla/5.0 AppleWebKit/537.36 Chrome/120.0 Safari/537.36",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }

    response = requests.get(url, headers=headers, timeout=25)
    response.raise_for_status()
    response.encoding = response.apparent_encoding
    return response.text


def load_sites_from_sheet():
    if not SHEET_CSV_URL:
        raise ValueError("SHEET_CSV_URL 没有设置")

    response = requests.get(SHEET_CSV_URL, timeout=25)
    response.raise_for_status()
    response.encoding = "utf-8-sig"

    reader = csv.DictReader(io.StringIO(response.text))

    sites = []

    for row in reader:
        enabled = clean_text(row.get("enabled", "")).lower()

        if enabled not in ["yes", "true", "1", "on", "开启", "是"]:
            continue

        site = {
            "name": clean_text(row.get("name", "")),
            "site_url": clean_text(row.get("site_url", "")),
            "base_url": clean_text(row.get("base_url", "")),
            "chat_id": clean_text(row.get("chat_id", "")),
            "seen_file": clean_text(row.get("seen_file", "")) or "seen.json",
            "max_articles": int(clean_text(row.get("max_articles", "")) or 5),
            "max_chars": int(clean_text(row.get("max_chars", "")) or 500),
            "send_image": clean_text(row.get("send_image", "")).lower() in ["yes", "true", "1", "on", "是"],
            "remove_domain": clean_text(row.get("remove_domain", "")),
        }

        if not site["site_url"] or not site["base_url"] or not site["chat_id"]:
            print("跳过配置不完整的网站：", site)
            continue

        sites.append(site)

    print(f"从表格读取到 {len(sites)} 个开启的网站")
    return sites


def fetch_articles(site):
    html = get_html(site["site_url"])
    soup = BeautifulSoup(html, "html.parser")

    articles = []

    skip_words = [
        "首页",
        "更多",
        "搜索",
        "导航",
        "联系我们",
        "阅读更多",
        "XML地图",
        "English",
        "EN",
        "专题",
        "标签",
        "热门",
        "排行榜",
        "快速链接",
        "新闻分类",
        "美食专题",
        "旅游专题",
        "上一篇",
        "下一篇",
    ]

    category_words = [
        "东盟新闻",
        "社会新闻",
        "国际新闻",
        "文旅频道",
        "财经新闻",
        "娱乐新闻",
        "国内新闻",
        "南亚视窗",
        "图行天下",
        "美食特产",
        "泰国",
        "越南",
        "柬埔寨",
        "印尼",
        "马来西亚",
        "新加坡",
        "老挝",
        "缅甸",
        "菲律宾",
        "文莱",
    ]

    base_domain = site["base_url"].replace("https://", "").replace("http://", "").strip("/")

    for a in soup.find_all("a", href=True):
        title = clean_text(a.get_text())
        href = a.get("href", "")

        if len(title) < 10:
            continue

        if any(word in title for word in skip_words):
            continue

        if title in category_words:
            continue

        link = urljoin(site["base_url"], href)

        if base_domain not in link:
            continue

        bad_links = [
            "/tags/",
            "/tag/",
            "/search",
            "/sitemap",
            "/list/",
            "/category/",
            "/about",
            "/contact",
            "#",
        ]

        if any(bad in link.lower() for bad in bad_links):
            continue

        path = link.replace(site["base_url"], "").strip("/")

        if not path or len(path) < 5:
            continue

        articles.append({
            "title": title,
            "link": link
        })

    unique = []
    used = set()

    for item in articles:
        if item["link"] not in used:
            unique.append(item)
            used.add(item["link"])

    print(f"[{site['name']}] 找到 {len(unique)} 篇文章")

    for item in unique[:site["max_articles"]]:
        print("文章：", item["title"], item["link"])

    return unique[:site["max_articles"]]


def get_summary(article_url, site):
    try:
        html = get_html(article_url)
        soup = BeautifulSoup(html, "html.parser")

        paragraphs = []

        bad_words = [
            "广告",
            "订阅",
            "更多消息",
            "延伸阅读",
            "相关新闻",
            "版权所有",
            "关注我们",
            "免责声明",
            "责任编辑",
            "来源",
            "上一篇",
            "下一篇",
            "分享到",
            "微信",
            "微博",
            "打印",
            "收藏",
        ]

        for p in soup.find_all("p"):
            text = clean_text(p.get_text())
            text = remove_domain_links(text, site["remove_domain"])

            if len(text) < 15:
                continue

            if any(word in text for word in bad_words):
                continue

            paragraphs.append(text)

        if paragraphs:
            content = " ".join(paragraphs)
            content = remove_domain_links(content, site["remove_domain"])
            return content[:site["max_chars"]]

        desc = soup.find("meta", attrs={"name": "description"})
        if desc and desc.get("content"):
            content = clean_text(desc.get("content"))
            content = remove_domain_links(content, site["remove_domain"])
            return content[:site["max_chars"]]

        og_desc = soup.find("meta", attrs={"property": "og:description"})
        if og_desc and og_desc.get("content"):
            content = clean_text(og_desc.get("content"))
            content = remove_domain_links(content, site["remove_domain"])
            return content[:site["max_chars"]]

        return "暂无更多内容。"

    except Exception as e:
        print("获取内容失败：", e)
        return "暂无更多内容。"


def is_unsupported_image(image_url):
    """
    Telegram sendPhoto 不支持 svg。
    这里过滤 svg、ico 等不适合当照片发送的格式。
    """
    if not image_url:
        return True

    url = image_url.lower().split("?")[0]

    unsupported_exts = [
        ".svg",
        ".ico",
        ".bmp",
        ".webmanifest",
    ]

    if any(url.endswith(ext) for ext in unsupported_exts):
        return True

    return False


def is_bad_image(image_url):
    image_url = (image_url or "").lower()

    if is_unsupported_image(image_url):
        return True

    bad_keywords = [
        "logo",
        "icon",
        "favicon",
        "qrcode",
        "qr",
        "wechat",
        "weixin",
        "avatar",
        "default",
        "banner",
        "ad",
        "ads",
        "loading",
        "placeholder",
        "blank",
        "sprite",
        "newspost",
    ]

    return any(word in image_url for word in bad_keywords)


def get_image(article_url, site):
    try:
        html = get_html(article_url)
        soup = BeautifulSoup(html, "html.parser")

        image_candidates = []

        og_image = soup.find("meta", attrs={"property": "og:image"})
        if og_image and og_image.get("content"):
            image_candidates.append(og_image.get("content"))

        twitter_image = soup.find("meta", attrs={"name": "twitter:image"})
        if twitter_image and twitter_image.get("content"):
            image_candidates.append(twitter_image.get("content"))

        for img in soup.find_all("img"):
            src = (
                img.get("src")
                or img.get("data-src")
                or img.get("data-original")
                or img.get("data-lazy-src")
            )

            if src:
                image_candidates.append(src)

        for src in image_candidates:
            image_url = urljoin(site["base_url"], src)

            if is_bad_image(image_url):
                print("跳过不合适图片：", image_url)
                continue

            print("使用图片：", image_url)
            return image_url

        print("没有找到合适图片")
        return None

    except Exception as e:
        print("获取图片失败：", e)
        return None


def send_text_to_telegram(chat_id, text, remove_domain=""):
    text = remove_domain_links(text, remove_domain)

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    response = requests.post(url, data={
        "chat_id": chat_id,
        "text": text[:3900],
        "disable_web_page_preview": True,
    }, timeout=25)

    print("Telegram 状态：", response.status_code)
    print("Telegram 返回：", response.text)

    response.raise_for_status()


def send_to_telegram(site, title, summary, image_url=None):
    title = remove_domain_links(title, site["remove_domain"])
    summary = remove_domain_links(summary, site["remove_domain"])

    caption = f"""📰 {title}

{summary}
"""

    caption = remove_domain_links(caption, site["remove_domain"])

    if site["send_image"] and image_url and not is_unsupported_image(image_url):
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"

        response = requests.post(url, data={
            "chat_id": site["chat_id"],
            "photo": image_url,
            "caption": caption[:1000],
        }, timeout=25)

        print("发布到频道：", site["chat_id"])
        print("Telegram 频道状态：", response.status_code)
        print("Telegram 频道返回：", response.text)

        if response.status_code == 200:
            return

        print("图片发送失败，改发文字")

    else:
        if image_url:
            print("图片格式不支持，跳过图片：", image_url)

    send_text_to_telegram(site["chat_id"], caption, site["remove_domain"])


def process_site(site):
    print("=" * 50)
    print("开始处理网站：", site["name"])

    seen = load_seen(site["seen_file"])
    articles = fetch_articles(site)

    count = 0

    for article in reversed(articles):
        title = article["title"]
        link = article["link"]

        if link in seen:
            continue

        print("准备发布：", title)

        summary = get_summary(link, site)
        image_url = get_image(link, site) if site["send_image"] else None

        print("标题：", title)
        print("内容字数：", len(summary))
        print("图片：", image_url)

        send_to_telegram(site, title, summary, image_url)

        seen.add(link)
        count += 1

        time.sleep(2)

    save_seen(site["seen_file"], seen)

    print(f"[{site['name']}] 完成，本次发布 {count} 篇")


def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN 没有设置")

    sites = load_sites_from_sheet()

    for site in sites:
        try:
            process_site(site)
        except Exception as e:
            print(f"处理网站失败：{site.get('name')}，错误：{e}")


if __name__ == "__main__":
    main()
