import os
import json
import re
import csv
import time
from io import StringIO
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


# =========================
# 环境变量
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
SHEET_CSV_URL = os.getenv("SHEET_CSV_URL", "").strip()

# 如果表格里面没写 chat_id，就会使用 GitHub Secrets 里的 CHAT_ID
DEFAULT_CHAT_IDS = [
    x.strip()
    for x in os.getenv("CHAT_ID", "").split(",")
    if x.strip()
]


# =========================
# 基础工具函数
# =========================

def to_bool(value, default=False):
    if value is None:
        return default
    value = str(value).strip().lower()
    if value in ["yes", "y", "true", "1", "on", "启用", "是"]:
        return True
    if value in ["no", "n", "false", "0", "off", "停用", "否"]:
        return False
    return default


def to_int(value, default):
    try:
        if value is None or str(value).strip() == "":
            return default
        return int(float(str(value).strip()))
    except Exception:
        return default


def split_list(value):
    """
    支持多个值：
    - 英文逗号 ,
    - 中文逗号 ，
    - 分号 ;
    - 竖线 |
    - 换行
    """
    if value is None:
        return []

    text = str(value).strip()
    if not text:
        return []

    parts = re.split(r"[,，;；|\n]+", text)
    return [x.strip() for x in parts if x.strip()]


def get_domain_from_url(url):
    try:
        host = urlparse(url).netloc
        return host.replace("www.", "")
    except Exception:
        return ""


def clean_text(text, remove_domain=""):
    text = text or ""

    # 去掉网址
    text = re.sub(r"https?://\S+", "", text)

    # 去掉表格里指定的域名
    for domain in split_list(remove_domain):
        domain = domain.strip()
        if domain:
            text = text.replace(domain, "")
            text = text.replace("www." + domain, "")

    # 去掉多余空白
    text = text.replace("\u3000", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


def safe_filename(name, default="seen.json"):
    name = str(name or "").strip()
    if not name:
        return default

    # 防止写到奇怪目录
    name = name.replace("\\", "_").replace("/", "_")

    if not name.endswith(".json"):
        name += ".json"

    return name


def get_html(url):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;"
            "q=0.9,image/avif,image/webp,*/*;q=0.8"
        ),
    }

    response = requests.get(url, headers=headers, timeout=25)
    response.raise_for_status()
    return response.text


# =========================
# 读取 Google 表格
# =========================

def normalize_row(row):
    """
    Google Sheet 表头可能有空格或大小写，这里统一处理。
    """
    new_row = {}
    for key, value in row.items():
        if key is None:
            continue
        clean_key = str(key).strip().lower().replace("\ufeff", "")
        new_row[clean_key] = value
    return new_row


def load_sites_from_sheet():
    if not SHEET_CSV_URL:
        raise ValueError("SHEET_CSV_URL 没有设置，请在 GitHub Secrets 里添加 SHEET_CSV_URL")

    print("正在读取 Google 表格配置...")

    response = requests.get(SHEET_CSV_URL, timeout=30)
    response.raise_for_status()

    # 处理 UTF-8 BOM
    text = response.text.replace("\ufeff", "")

    reader = csv.DictReader(StringIO(text))
    sites = []

    for raw_row in reader:
        row = normalize_row(raw_row)

        enabled = to_bool(row.get("enabled"), default=False)
        if not enabled:
            continue

        name = str(row.get("name", "")).strip() or "未命名网站"
        site_url = str(row.get("site_url", "")).strip()
        base_url = str(row.get("base_url", "")).strip()

        if not site_url:
            print(f"跳过 {name}：site_url 为空")
            continue

        if not base_url:
            parsed = urlparse(site_url)
            base_url = f"{parsed.scheme}://{parsed.netloc}"

        chat_ids = split_list(row.get("chat_id"))
        if not chat_ids:
            chat_ids = DEFAULT_CHAT_IDS

        if not chat_ids:
            print(f"跳过 {name}：chat_id 为空，表格和 GitHub Secrets 都没有频道 ID")
            continue

        site = {
            "enabled": enabled,
            "name": name,
            "site_url": site_url,
            "base_url": base_url,
            "chat_ids": chat_ids,
            "seen_file": safe_filename(row.get("seen_file"), "seen.json"),
            "max_articles": to_int(row.get("max_articles"), 30),
            "max_chars": to_int(row.get("max_chars"), 500),
            "send_image": to_bool(row.get("send_image"), default=True),
            "remove_domain": str(row.get("remove_domain", "")).strip(),
            "min_title_len": to_int(row.get("min_title_len"), 8),
        }

        # 自动把 base_url 的域名也加入清理范围
        base_domain = get_domain_from_url(base_url)
        if base_domain:
            if site["remove_domain"]:
                site["remove_domain"] += "," + base_domain
            else:
                site["remove_domain"] = base_domain

        sites.append(site)

    print(f"从表格读取到 {len(sites)} 个开启的网站")
    return sites


# =========================
# seen 记录
# =========================

def load_seen(seen_file):
    if not os.path.exists(seen_file):
        return set()

    try:
        with open(seen_file, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data)
    except Exception:
        return set()


def save_seen(seen_file, seen):
    try:
        with open(seen_file, "w", encoding="utf-8") as f:
            json.dump(list(seen)[-1000:], f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"保存 {seen_file} 失败：{e}")


# =========================
# 抓取文章列表
# =========================

def is_bad_link(link):
    bad_words = [
        "javascript:",
        "mailto:",
        "tel:",
        "#",
        "/login",
        "/register",
        "/signup",
        "/subscribe",
        "/privacy",
        "/terms",
        "/about",
        "/contact",
        "/advertise",
    ]

    lower = link.lower()
    return any(word in lower for word in bad_words)


def same_domain_or_relative(link, base_url):
    try:
        link_host = urlparse(link).netloc.replace("www.", "")
        base_host = urlparse(base_url).netloc.replace("www.", "")

        if not link_host:
            return True

        return link_host == base_host or link_host.endswith("." + base_host)
    except Exception:
        return True


def fetch_articles(site):
    site_url = site["site_url"]
    base_url = site["base_url"]
    min_title_len = site["min_title_len"]
    max_articles = site["max_articles"]
    remove_domain = site["remove_domain"]

    html = get_html(site_url)
    soup = BeautifulSoup(html, "html.parser")

    articles = []

    for a in soup.find_all("a", href=True):
        title = clean_text(a.get_text(" ", strip=True), remove_domain)
        href = str(a.get("href", "")).strip()

        if len(title) < min_title_len:
            continue

        if is_bad_link(href):
            continue

        link = urljoin(base_url, href)

        if not link.startswith("http"):
            continue

        if not same_domain_or_relative(link, base_url):
            continue

        articles.append({
            "title": title,
            "link": link,
        })

    unique = []
    used_links = set()
    used_titles = set()

    for item in articles:
        link = item["link"].split("#")[0]
        title = item["title"]

        if link in used_links:
            continue

        # 避免导航栏重复标题
        title_key = re.sub(r"\s+", "", title)
        if title_key in used_titles:
            continue

        used_links.add(link)
        used_titles.add(title_key)

        unique.append({
            "title": title,
            "link": link,
        })

    print(f"[{site['name']}] 找到 {len(unique)} 篇文章")
    return unique[:max_articles]


# =========================
# 抓取正文摘要
# =========================

def remove_bad_paragraph(text):
    if not text:
        return True

    bad_keywords = [
        "版权所有",
        "Copyright",
        "All Rights Reserved",
        "免责声明",
        "广告",
        "ADVERTISEMENT",
        "订阅",
        "登录",
        "注册",
        "分享",
        "扫一扫",
        "扫码",
        "更多精彩内容",
        "下载客户端",
        "关注我们",
    ]

    lower = text.lower()

    for word in bad_keywords:
        if word.lower() in lower:
            return True

    return False


def format_summary(paragraphs, max_chars=500, remove_domain=""):
    clean_paragraphs = []
    total_len = 0

    max_chars = max(100, min(int(max_chars), 1000))

    for p in paragraphs:
        p = clean_text(p, remove_domain)

        if not p:
            continue

        if len(p) < 15:
            continue

        if remove_bad_paragraph(p):
            continue

        # 如果加上这一段超过字数，就截断
        if total_len + len(p) > max_chars:
            remaining = max_chars - total_len

            if remaining >= 60:
                cut_text = p[:remaining]

                # 尽量在标点处截断
                last_punc = max(
                    cut_text.rfind("。"),
                    cut_text.rfind("！"),
                    cut_text.rfind("？"),
                    cut_text.rfind("."),
                    cut_text.rfind("!"),
                    cut_text.rfind("?"),
                )

                if last_punc > 40:
                    cut_text = cut_text[:last_punc + 1]

                clean_paragraphs.append(cut_text)

            break

        clean_paragraphs.append(p)
        total_len += len(p)

        # 够 300 字左右就可以，不强行继续
        if total_len >= min(300, max_chars):
            break

    if not clean_paragraphs:
        return "暂无更多内容。"

    return "\n\n".join(clean_paragraphs).strip()


def get_meta_content(soup, name=None, prop=None):
    if name:
        tag = soup.find("meta", attrs={"name": name})
        if tag and tag.get("content"):
            return tag.get("content")

    if prop:
        tag = soup.find("meta", attrs={"property": prop})
        if tag and tag.get("content"):
            return tag.get("content")

    return ""


def get_summary(article_url, site):
    remove_domain = site["remove_domain"]
    max_chars = site["max_chars"]

    try:
        html = get_html(article_url)
        soup = BeautifulSoup(html, "html.parser")

        paragraphs = []

        # 优先从 article 标签里找
        article_tag = soup.find("article")
        if article_tag:
            for p in article_tag.find_all("p"):
                text = clean_text(p.get_text(" ", strip=True), remove_domain)
                if len(text) >= 15:
                    paragraphs.append(text)

        # 再从常见正文区域找
        if not paragraphs:
            selectors = [
                "main p",
                ".article p",
                ".article-content p",
                ".content p",
                ".entry-content p",
                ".post-content p",
                ".story-content p",
                ".news-content p",
                "#article-content p",
            ]

            for selector in selectors:
                for p in soup.select(selector):
                    text = clean_text(p.get_text(" ", strip=True), remove_domain)
                    if len(text) >= 15:
                        paragraphs.append(text)

                if paragraphs:
                    break

        # 最后全站 p 标签兜底
        if not paragraphs:
            for p in soup.find_all("p"):
                text = clean_text(p.get_text(" ", strip=True), remove_domain)
                if len(text) >= 20:
                    paragraphs.append(text)

        if paragraphs:
            return format_summary(paragraphs, max_chars=max_chars, remove_domain=remove_domain)

        # meta description 兜底
        desc = (
            get_meta_content(soup, name="description")
            or get_meta_content(soup, prop="og:description")
            or get_meta_content(soup, name="twitter:description")
        )

        if desc:
            return format_summary([desc], max_chars=max_chars, remove_domain=remove_domain)

        return "暂无更多内容。"

    except Exception as e:
        print("获取内容失败：", article_url, e)
        return "暂无更多内容。"


# =========================
# 抓取图片
# =========================

def pick_from_srcset(srcset):
    if not srcset:
        return ""

    # srcset 一般是：url1 300w, url2 600w
    parts = srcset.split(",")
    if not parts:
        return ""

    first = parts[-1].strip().split(" ")[0].strip()
    return first


def is_bad_image_url(img_url):
    if not img_url:
        return True

    lower = img_url.lower()

    bad_words = [
        "logo",
        "icon",
        "avatar",
        "default",
        "placeholder",
        "social-share",
        "sprite",
        "blank",
        "transparent",
        "loading",
    ]

    if any(word in lower for word in bad_words):
        return True

    # Telegram sendPhoto 经常不接受 svg，这就是你之前 400 的常见原因
    bad_exts = [".svg", ".gif"]
    if any(lower.split("?")[0].endswith(ext) for ext in bad_exts):
        return True

    return False


def get_img_url_from_tag(img):
    img_url = (
        img.get("src")
        or img.get("data-src")
        or img.get("data-original")
        or img.get("data-lazy-src")
        or img.get("data-url")
        or pick_from_srcset(img.get("srcset"))
        or pick_from_srcset(img.get("data-srcset"))
    )

    return str(img_url or "").strip()


def get_image(article_url, site):
    base_url = site["base_url"]

    try:
        html = get_html(article_url)
        soup = BeautifulSoup(html, "html.parser")

        # 优先正文图片
        article_tag = soup.find("article")
        if article_tag:
            for img in article_tag.find_all("img"):
                img_url = get_img_url_from_tag(img)
                if not img_url:
                    continue

                img_url = urljoin(base_url, img_url)

                if is_bad_image_url(img_url):
                    continue

                return img_url

        # 常见正文区域图片
        selectors = [
            "main img",
            ".article img",
            ".article-content img",
            ".content img",
            ".entry-content img",
            ".post-content img",
            ".story-content img",
            ".news-content img",
        ]

        for selector in selectors:
            for img in soup.select(selector):
                img_url = get_img_url_from_tag(img)
                if not img_url:
                    continue

                img_url = urljoin(base_url, img_url)

                if is_bad_image_url(img_url):
                    continue

                return img_url

        # og:image 兜底
        meta_images = [
            get_meta_content(soup, prop="og:image"),
            get_meta_content(soup, name="twitter:image"),
            get_meta_content(soup, name="image"),
        ]

        for img_url in meta_images:
            if not img_url:
                continue

            img_url = urljoin(base_url, img_url)

            if is_bad_image_url(img_url):
                continue

            return img_url

        return None

    except Exception as e:
        print("获取图片失败：", article_url, e)
        return None


# =========================
# 发送 Telegram
# =========================

def send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    response = requests.post(
        url,
        data={
            "chat_id": chat_id,
            "text": text[:4000],
            "disable_web_page_preview": True,
        },
        timeout=30,
    )

    print(f"Telegram 频道 {chat_id} 文字状态：", response.status_code)
    print(f"Telegram 频道 {chat_id} 文字返回：", response.text)

    return response.status_code == 200


def send_photo(chat_id, photo_url, caption):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"

    response = requests.post(
        url,
        data={
            "chat_id": chat_id,
            "photo": photo_url,
            "caption": caption[:1000],
        },
        timeout=30,
    )

    print(f"Telegram 频道 {chat_id} 图片状态：", response.status_code)
    print(f"Telegram 频道 {chat_id} 图片返回：", response.text)

    return response.status_code == 200


def send_to_telegram(title, summary, image_url=None, chat_id=None):
    caption = f"📰 {title}\n\n{summary}".strip()

    if not chat_id:
        print("发送失败：chat_id 为空")
        return False

    # 先尝试发图片
    if image_url:
        ok = send_photo(chat_id, image_url, caption)

        if ok:
            return True

        # 图片失败时，不中断程序，自动改发文字
        print("图片发送失败，自动改为纯文字发送")

    return send_message(chat_id, caption)


# =========================
# 主流程
# =========================

def process_site(site):
    print("=" * 60)
    print(f"开始处理网站：{site['name']}")
    print(f"列表链接：{site['site_url']}")
    print(f"频道数量：{len(site['chat_ids'])}")
    print(f"最多抓取：{site['max_articles']} 篇")
    print(f"摘要字数：{site['max_chars']}")
    print(f"是否发图：{site['send_image']}")
    print("=" * 60)

    seen_file = site["seen_file"]
    seen = load_seen(seen_file)

    try:
        articles = fetch_articles(site)
    except Exception as e:
        print(f"[{site['name']}] 抓取文章列表失败：{e}")
        return 0

    count = 0

    # reversed 是为了从旧到新发，避免每次顺序太乱
    for article in reversed(articles):
        title = article["title"]
        link = article["link"]

        if link in seen:
            continue

        summary = get_summary(link, site)

        image_url = None
        if site["send_image"]:
            image_url = get_image(link, site)

        chat_id = site["chat_ids"][count % len(site["chat_ids"])]

        print("-" * 60)
        print("标题：", title)
        print("链接：", link)
        print("内容字数：", len(summary.replace("\n", "")))
        print("图片：", image_url)
        print("发布到频道：", chat_id)

        ok = send_to_telegram(
            title=title,
            summary=summary,
            image_url=image_url,
            chat_id=chat_id,
        )

        if ok:
            seen.add(link)
            count += 1
            save_seen(seen_file, seen)
            print("发送成功")
        else:
            print("发送失败，本篇不会加入 seen，下次还会重试")

        # 防止发送太快
        time.sleep(2)

    save_seen(seen_file, seen)

    print(f"[{site['name']}] 完成，本次发布 {count} 篇")
    return count


def main():
    if not BOT_TOKEN:
        print("错误：没有设置 BOT_TOKEN")
        return

    try:
        sites = load_sites_from_sheet()
    except Exception as e:
        print("读取 Google 表格失败：", e)
        return

    if not sites:
        print("没有可用网站。请检查 Google 表格 enabled 是否为 yes，表头是否正确。")
        return

    total_count = 0

    for site in sites:
        total_count += process_site(site)

    print("=" * 60)
    print(f"全部完成，本次总共发布 {total_count} 篇")
    print("=" * 60)


if __name__ == "__main__":
    main()
