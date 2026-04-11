"""
GSC MCP Server — Enhanced Fork
Google Search Console + Indexing API + Core Web Vitals integration for MCP.
"""

# ── Auto-activate .venv if running from system Python (e.g. Glama Docker) ────
import os, sys, site, glob
_venv = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".venv")
if os.path.isdir(_venv) and _venv not in sys.prefix:
    # Find any site-packages dir in the venv (version may differ from running Python)
    for _sp in glob.glob(os.path.join(_venv, "lib", "python*", "site-packages")):
        site.addsitedir(_sp)
    # Windows layout
    _sp_win = os.path.join(_venv, "Lib", "site-packages")
    if os.path.isdir(_sp_win):
        site.addsitedir(_sp_win)

from typing import Any, Dict, List, Optional, Set, Tuple
import logging
import json
import asyncio
import time
import math
import gzip
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from collections import Counter, deque
from html import unescape
from urllib.parse import urlparse, urljoin, urlunsplit
from datetime import datetime, timedelta

import google.auth
from google.auth.transport.requests import Request
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import httpx
from bs4 import BeautifulSoup

# Suppress the noisy file_cache warning from google-api-python-client.
logging.getLogger("googleapiclient.discovery_cache").setLevel(logging.ERROR)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("mcp-seo-audit")

# ──────────────────────────────────────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

GSC_CREDENTIALS_PATH = os.environ.get("GSC_CREDENTIALS_PATH")
POSSIBLE_CREDENTIAL_PATHS = [
    GSC_CREDENTIALS_PATH,
    os.path.join(SCRIPT_DIR, "service_account_credentials.json"),
    os.path.join(os.getcwd(), "service_account_credentials.json"),
]

OAUTH_CLIENT_SECRETS_FILE = os.environ.get("GSC_OAUTH_CLIENT_SECRETS_FILE")
if not OAUTH_CLIENT_SECRETS_FILE:
    OAUTH_CLIENT_SECRETS_FILE = os.path.join(SCRIPT_DIR, "client_secrets.json")

TOKEN_FILE = os.path.join(SCRIPT_DIR, "token.json")
SKIP_OAUTH = os.environ.get("GSC_SKIP_OAUTH", "").lower() in ("true", "1", "yes")

_raw_data_state = os.environ.get("GSC_DATA_STATE", "all").lower().strip()
if _raw_data_state not in ("all", "final"):
    raise ValueError(
        f"Invalid GSC_DATA_STATE value '{_raw_data_state}'. "
        "Accepted values are 'all' (default, matches GSC dashboard) or 'final' (2-3 day lag)."
    )
DATA_STATE = _raw_data_state

# GSC API scope (read/write for sitemaps, site management)
GSC_SCOPES = ["https://www.googleapis.com/auth/webmasters"]

# Indexing API scope (separate from GSC)
INDEXING_SCOPES = ["https://www.googleapis.com/auth/indexing"]

# CrUX API key (free, no OAuth needed)
CRUX_API_KEY = os.environ.get("CRUX_API_KEY", "")

# PageSpeed Insights API key (optional, but recommended for stable quota)
PAGESPEED_API_KEY = os.environ.get("PAGESPEED_API_KEY", os.environ.get("GOOGLE_API_KEY", ""))

# Optional explicit Chrome path for local Lighthouse runs
LIGHTHOUSE_CHROME_PATH = os.environ.get("LIGHTHOUSE_CHROME_PATH", os.environ.get("CHROME_PATH", ""))

DEFAULT_FETCH_HEADERS = {
    "User-Agent": "mcp-seo-audit/2.0 (+https://github.com/GiorgiKemo/mcp-seo-audit)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.5",
}
DEFAULT_FETCH_TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=20.0, pool=20.0)
LIGHTHOUSE_CATEGORIES = {"performance", "accessibility", "best-practices", "seo", "pwa"}

# ──────────────────────────────────────────────────────────────────────────────
# Authentication helpers
# ──────────────────────────────────────────────────────────────────────────────

_gsc_service_cache = None
_indexing_service_cache = None


def get_gsc_service():
    """Returns an authorized Search Console service object (cached)."""
    global _gsc_service_cache
    if _gsc_service_cache is not None:
        return _gsc_service_cache

    if not SKIP_OAUTH:
        try:
            svc = get_gsc_service_oauth()
            _gsc_service_cache = svc
            return svc
        except Exception:
            pass

    for cred_path in POSSIBLE_CREDENTIAL_PATHS:
        if cred_path and os.path.exists(cred_path):
            try:
                creds = service_account.Credentials.from_service_account_file(
                    cred_path, scopes=GSC_SCOPES
                )
                svc = build("searchconsole", "v1", credentials=creds, cache_discovery=False)
                _gsc_service_cache = svc
                return svc
            except Exception:
                continue

    raise FileNotFoundError(
        "Authentication failed. Please either:\n"
        "1. Set up OAuth by placing a client_secrets.json file in the script directory, or\n"
        "2. Set the GSC_CREDENTIALS_PATH environment variable or place a service account credentials file."
    )


def get_gsc_service_oauth():
    """Returns an authorized Search Console service object using OAuth."""
    creds = None

    if os.path.exists(TOKEN_FILE):
        try:
            creds = Credentials.from_authorized_user_file(TOKEN_FILE, GSC_SCOPES)
        except Exception:
            if os.path.exists(TOKEN_FILE):
                os.remove(TOKEN_FILE)
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                with open(TOKEN_FILE, "w") as token:
                    token.write(creds.to_json())
            except Exception:
                if os.path.exists(TOKEN_FILE):
                    os.remove(TOKEN_FILE)
                creds = None

        if not creds or not creds.valid:
            if not os.path.exists(OAUTH_CLIENT_SECRETS_FILE):
                raise FileNotFoundError(
                    "OAuth client secrets file not found. Please place a client_secrets.json "
                    "file in the script directory or set GSC_OAUTH_CLIENT_SECRETS_FILE."
                )
            flow = InstalledAppFlow.from_client_secrets_file(OAUTH_CLIENT_SECRETS_FILE, GSC_SCOPES)
            creds = flow.run_local_server(port=0)
            with open(TOKEN_FILE, "w") as token:
                token.write(creds.to_json())

    return build("searchconsole", "v1", credentials=creds, cache_discovery=False)


def get_indexing_service():
    """Returns an authorized Indexing API service object (cached).
    Uses OAuth credentials with indexing scope, or service account."""
    global _indexing_service_cache
    if _indexing_service_cache is not None:
        return _indexing_service_cache

    # Try service account first (recommended for Indexing API)
    for cred_path in POSSIBLE_CREDENTIAL_PATHS:
        if cred_path and os.path.exists(cred_path):
            try:
                creds = service_account.Credentials.from_service_account_file(
                    cred_path, scopes=INDEXING_SCOPES
                )
                svc = build("indexing", "v3", credentials=creds, cache_discovery=False)
                _indexing_service_cache = svc
                return svc
            except Exception:
                continue

    # Fall back to OAuth with indexing scope
    if os.path.exists(TOKEN_FILE):
        try:
            with open(TOKEN_FILE, "r") as f:
                token_data = json.load(f)
            with open(OAUTH_CLIENT_SECRETS_FILE, "r") as f:
                client_data = json.load(f)

            client_config = client_data.get("installed", client_data.get("web", {}))
            creds = Credentials(
                token=token_data.get("token"),
                refresh_token=token_data.get("refresh_token"),
                token_uri="https://oauth2.googleapis.com/token",
                client_id=client_config["client_id"],
                client_secret=client_config["client_secret"],
                scopes=INDEXING_SCOPES,
            )
            if creds.expired:
                creds.refresh(Request())
            svc = build("indexing", "v3", credentials=creds, cache_discovery=False)
            _indexing_service_cache = svc
            return svc
        except Exception:
            pass

    raise FileNotFoundError(
        "Indexing API authentication failed. The Indexing API requires a service account "
        "with indexing permissions, or OAuth credentials with the indexing scope."
    )


def _site_not_found_error(site_url: str) -> str:
    """Return a helpful message when a GSC property returns 404."""
    lines = [f"Property '{site_url}' not found (404). Possible causes:\n"]
    lines.append(
        "1. The site_url doesn't exactly match what is in GSC. "
        "Run list_properties to get the exact string to use."
    )
    if site_url.startswith("sc-domain:"):
        lines.append(
            "2. Domain properties require the service account to be explicitly added "
            "under GSC Settings > Users and permissions for that specific domain property."
        )
    else:
        lines.append(
            "2. If your property is a domain property (covers all subdomains), "
            "the correct format is 'sc-domain:example.com', not a full URL."
        )
    lines.append("3. The authenticated account may not have access to this property.")
    return "\n".join(lines)


def _ensure_https_url(url_or_origin: str) -> str:
    """Normalize a property/origin/url into a fetchable HTTPS URL."""
    value = (url_or_origin or "").strip()
    if not value:
        raise ValueError("A URL or origin is required.")
    if value.startswith("sc-domain:"):
        return f"https://{value.split(':', 1)[1].strip('/')}"
    parsed = urlparse(value)
    if not parsed.scheme:
        return f"https://{value.lstrip('/')}"
    return value


def _origin_from_url(url_or_origin: str) -> str:
    url = _ensure_https_url(url_or_origin)
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _clean_text(value: Optional[str]) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", unescape(value)).strip()


def _clip(value: str, limit: int = 120) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def _status_class(status_code: int) -> str:
    if 200 <= status_code < 300:
        return "ok"
    if 300 <= status_code < 400:
        return "redirect"
    if 400 <= status_code < 500:
        return "client_error"
    if status_code >= 500:
        return "server_error"
    return "unknown"


def _canonicalize_crawl_url(base_url: str, href: str) -> Optional[str]:
    if not href:
        return None
    absolute = urljoin(base_url, href)
    parsed = urlparse(absolute)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", "", ""))


async def _fetch_url(
    url: str,
    *,
    method: str = "GET",
    follow_redirects: bool = True,
    headers: Optional[Dict[str, str]] = None,
) -> httpx.Response:
    request_headers = dict(DEFAULT_FETCH_HEADERS)
    if headers:
        request_headers.update(headers)

    async with httpx.AsyncClient(
        follow_redirects=follow_redirects,
        timeout=DEFAULT_FETCH_TIMEOUT,
        headers=request_headers,
    ) as client:
        response = await client.request(method, url)
        return response


def _extract_json_ld_types(value: Any, types: Set[str]) -> None:
    if isinstance(value, dict):
        schema_type = value.get("@type")
        if isinstance(schema_type, list):
            for item in schema_type:
                if item:
                    types.add(str(item))
        elif schema_type:
            types.add(str(schema_type))
        for nested in value.values():
            _extract_json_ld_types(nested, types)
    elif isinstance(value, list):
        for item in value:
            _extract_json_ld_types(item, types)


def _parse_json_ld_types(soup: BeautifulSoup) -> List[str]:
    discovered: Set[str] = set()
    for script in soup.find_all("script"):
        script_type = (script.get("type") or "").lower()
        if "ld+json" not in script_type:
            continue
        raw = script.string or script.get_text(" ", strip=True)
        raw = raw.strip()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            cleaned = raw.replace("<!--", "").replace("-->", "").strip()
            try:
                parsed = json.loads(cleaned)
            except Exception:
                continue
        _extract_json_ld_types(parsed, discovered)
    return sorted(discovered)


def _parse_meta_tags(soup: BeautifulSoup) -> Dict[str, str]:
    meta: Dict[str, str] = {}
    for tag in soup.find_all("meta"):
        key = (tag.get("name") or tag.get("property") or "").strip().lower()
        value = _clean_text(tag.get("content"))
        if key and value and key not in meta:
            meta[key] = value
    return meta


def _analyze_html_document(final_url: str, status_code: int, headers: Dict[str, str], html: str) -> Dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    meta = _parse_meta_tags(soup)

    title_text = _clean_text(soup.title.get_text(" ", strip=True) if soup.title else "")
    canonical_tags = []
    for link in soup.find_all("link"):
        rel = link.get("rel") or []
        rel_values = [str(item).lower() for item in (rel if isinstance(rel, list) else [rel])]
        if "canonical" in rel_values:
            href = _clean_text(link.get("href"))
            if href:
                canonical_tags.append(href)

    hreflangs = []
    for link in soup.find_all("link"):
        hreflang = _clean_text(link.get("hreflang"))
        href = _clean_text(link.get("href"))
        if hreflang and href:
            hreflangs.append((hreflang, href))

    headings = {
        "h1": [_clean_text(tag.get_text(" ", strip=True)) for tag in soup.find_all("h1") if _clean_text(tag.get_text(" ", strip=True))],
        "h2": [_clean_text(tag.get_text(" ", strip=True)) for tag in soup.find_all("h2") if _clean_text(tag.get_text(" ", strip=True))],
    }

    body_text = _clean_text(soup.get_text(" ", strip=True))
    structured_types = _parse_json_ld_types(soup)
    x_robots = _clean_text(headers.get("x-robots-tag", ""))
    robots_meta = meta.get("robots", "")
    googlebot_meta = meta.get("googlebot", "")
    issues: List[str] = []
    notes: List[str] = []

    if status_code >= 400:
        issues.append(f"HTTP status {status_code}")
    if not title_text:
        issues.append("Missing <title>")
    elif len(title_text) > 60:
        notes.append(f"Title is long ({len(title_text)} chars)")
    elif len(title_text) < 15:
        notes.append(f"Title is short ({len(title_text)} chars)")

    description = meta.get("description", "")
    if not description:
        issues.append("Missing meta description")
    elif len(description) > 160:
        notes.append(f"Meta description is long ({len(description)} chars)")
    elif len(description) < 70:
        notes.append(f"Meta description is short ({len(description)} chars)")

    if len(canonical_tags) > 1:
        issues.append("Multiple canonical tags found")
    if canonical_tags:
        canonical_url = canonical_tags[0]
        if urlparse(canonical_url).netloc and urlparse(canonical_url).netloc != urlparse(final_url).netloc:
            notes.append(f"Canonical points to another host: {canonical_url}")
    else:
        notes.append("No canonical tag found")

    indexability_signals = " | ".join(part for part in [robots_meta, googlebot_meta, x_robots] if part).lower()
    if "noindex" in indexability_signals:
        issues.append("Page is explicitly marked noindex")

    if not headings["h1"]:
        issues.append("Missing H1")
    elif len(headings["h1"]) > 1:
        notes.append(f"Multiple H1 tags found ({len(headings['h1'])})")

    if not meta.get("og:title"):
        notes.append("Missing og:title")
    if not meta.get("og:description"):
        notes.append("Missing og:description")
    if not meta.get("og:image"):
        notes.append("Missing og:image")
    if not meta.get("twitter:card"):
        notes.append("Missing twitter:card")

    return {
        "title": title_text,
        "meta_description": description,
        "meta": meta,
        "canonicals": canonical_tags,
        "hreflangs": hreflangs,
        "headings": headings,
        "structured_types": structured_types,
        "x_robots_tag": x_robots,
        "issues": issues,
        "notes": notes,
        "body_word_count": len(body_text.split()) if body_text else 0,
    }


def _iter_internal_links(final_url: str, html: str) -> List[str]:
    soup = BeautifulSoup(html, "html.parser")
    parsed_final = urlparse(final_url)
    origin = f"{parsed_final.scheme}://{parsed_final.netloc}"
    links: List[str] = []
    seen: Set[str] = set()
    for anchor in soup.find_all("a", href=True):
        normalized = _canonicalize_crawl_url(final_url, anchor["href"])
        if not normalized:
            continue
        parsed = urlparse(normalized)
        if f"{parsed.scheme}://{parsed.netloc}" != origin:
            continue
        if normalized not in seen:
            seen.add(normalized)
            links.append(normalized)
    return links


def _extract_xml_text(response: httpx.Response) -> str:
    content = response.content
    content_type = response.headers.get("content-type", "").lower()
    if response.url.path.endswith(".gz") or "gzip" in content_type:
        try:
            content = gzip.decompress(content)
        except Exception:
            pass
    return content.decode(response.encoding or "utf-8", errors="replace")


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _parse_sitemap_document(xml_text: str) -> Dict[str, Any]:
    root = ET.fromstring(xml_text)
    root_name = _local_name(root.tag)
    if root_name == "sitemapindex":
        sitemaps = []
        for node in root.findall(".//"):
            if _local_name(node.tag) == "loc" and node.text:
                sitemaps.append(node.text.strip())
        return {"type": "sitemapindex", "sitemaps": sitemaps, "urls": []}

    if root_name == "urlset":
        urls = []
        lastmods = 0
        for url_node in root.findall(".//"):
            if _local_name(url_node.tag) == "url":
                loc = None
                lastmod = None
                for child in list(url_node):
                    child_name = _local_name(child.tag)
                    if child_name == "loc" and child.text:
                        loc = child.text.strip()
                    elif child_name == "lastmod" and child.text:
                        lastmod = child.text.strip()
                if loc:
                    urls.append({"loc": loc, "lastmod": lastmod})
                    if lastmod:
                        lastmods += 1
        return {"type": "urlset", "sitemaps": [], "urls": urls, "lastmod_count": lastmods}

    raise ValueError(f"Unsupported sitemap root element: {root_name}")


def _format_loading_experience(experience: Dict[str, Any], label: str) -> List[str]:
    if not experience:
        return [f"{label}: no field data"]
    lines = [f"{label}:"]
    for metric_name, metric in experience.get("metrics", {}).items():
        percentile = metric.get("percentile")
        category = metric.get("category", "N/A")
        lines.append(f"  {metric_name}: p75={percentile} ({category})")
    return lines


def _summarize_lighthouse_payload(payload: Dict[str, Any], source_label: str) -> str:
    result = payload.get("lighthouseResult", payload)
    categories = result.get("categories", {})
    audits = result.get("audits", {})
    lines = [source_label]

    if categories:
        lines.append("Category scores:")
        for key in ["performance", "seo", "accessibility", "best-practices", "pwa"]:
            category = categories.get(key)
            if not category:
                continue
            score = category.get("score")
            score_text = "N/A" if score is None else f"{round(score * 100)}"
            lines.append(f"  {key}: {score_text}")

    metric_ids = [
        "first-contentful-paint",
        "largest-contentful-paint",
        "speed-index",
        "interactive",
        "total-blocking-time",
        "cumulative-layout-shift",
    ]
    lines.append("Key metrics:")
    for audit_id in metric_ids:
        audit = audits.get(audit_id)
        if not audit:
            continue
        value = audit.get("displayValue") or audit.get("numericValue") or "N/A"
        lines.append(f"  {audit.get('title', audit_id)}: {value}")

    opportunities = []
    for audit_id, audit in audits.items():
        score = audit.get("score")
        savings_ms = 0
        details = audit.get("details") or {}
        if isinstance(details, dict):
            savings_ms = details.get("overallSavingsMs") or 0
        numeric_value = audit.get("numericValue") or 0
        if score is not None and score < 0.9:
            opportunities.append(
                (
                    savings_ms,
                    numeric_value,
                    audit.get("title", audit_id),
                    audit.get("displayValue", ""),
                )
            )
    opportunities.sort(key=lambda item: (item[0], item[1]), reverse=True)
    if opportunities:
        lines.append("Top opportunities / failing audits:")
        for _, _, title, display_value in opportunities[:8]:
            suffix = f" ({display_value})" if display_value else ""
            lines.append(f"  {title}{suffix}")

    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────────
# Property Management Tools
# ──────────────────────────────────────────────────────────────────────────────

@mcp.tool()
async def list_properties() -> str:
    """Retrieves and returns the user's Search Console properties."""
    try:
        service = get_gsc_service()
        site_list = service.sites().list().execute()
        sites = site_list.get("siteEntry", [])

        if not sites:
            return "No Search Console properties found."

        lines = []
        for site in sites:
            site_url = site.get("siteUrl", "Unknown")
            permission = site.get("permissionLevel", "Unknown permission")
            lines.append(f"- {site_url} ({permission})")

        return "\n".join(lines)
    except Exception as e:
        return f"Error retrieving properties: {str(e)}"


@mcp.tool()
async def add_site(site_url: str) -> str:
    """
    Add a site to your Search Console properties.

    Args:
        site_url: The URL of the site to add (e.g. https://example.com or sc-domain:example.com)
    """
    try:
        service = get_gsc_service()
        service.sites().add(siteUrl=site_url).execute()
        return f"Site {site_url} has been added to Search Console."
    except HttpError as e:
        error_code = e.resp.status
        if error_code == 409:
            return f"Site {site_url} is already added to Search Console."
        return f"Error adding site (HTTP {error_code}): {str(e)}"
    except Exception as e:
        return f"Error adding site: {str(e)}"


@mcp.tool()
async def delete_site(site_url: str) -> str:
    """
    Remove a site from your Search Console properties.

    Args:
        site_url: The URL of the site to remove
    """
    try:
        service = get_gsc_service()
        service.sites().delete(siteUrl=site_url).execute()
        return f"Site {site_url} has been removed from Search Console."
    except HttpError as e:
        if e.resp.status == 404:
            return f"Site {site_url} was not found in Search Console."
        return f"Error removing site (HTTP {e.resp.status}): {str(e)}"
    except Exception as e:
        return f"Error removing site: {str(e)}"


# ──────────────────────────────────────────────────────────────────────────────
# Search Analytics Tools
# ──────────────────────────────────────────────────────────────────────────────

@mcp.tool()
async def get_search_analytics(
    site_url: str,
    days: int = 28,
    dimensions: str = "query",
    row_limit: int = 20,
    search_type: str = "WEB",
) -> str:
    """
    Get search analytics data for a specific property.

    Args:
        site_url: Exact GSC property URL (e.g. "sc-domain:example.com")
        days: Number of days to look back (default: 28)
        dimensions: Dimensions to group by, comma-separated (query, page, device, country, date, searchAppearance)
        row_limit: Number of rows to return (default: 20, max: 500)
        search_type: Type of search results (WEB, IMAGE, VIDEO, NEWS, DISCOVER)
    """
    try:
        service = get_gsc_service()
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=days)
        dimension_list = [d.strip() for d in dimensions.split(",")]

        request = {
            "startDate": start_date.strftime("%Y-%m-%d"),
            "endDate": end_date.strftime("%Y-%m-%d"),
            "dimensions": dimension_list,
            "rowLimit": min(max(1, row_limit), 500),
            "searchType": search_type.upper(),
            "dataState": DATA_STATE,
        }

        response = service.searchanalytics().query(siteUrl=site_url, body=request).execute()

        if not response.get("rows"):
            return f"No search analytics data found for {site_url} in the last {days} days."

        result_lines = [f"Search analytics for {site_url} (last {days} days, type={search_type}):"]
        result_lines.append("-" * 80)

        header = [dim.capitalize() for dim in dimension_list] + ["Clicks", "Impressions", "CTR", "Position"]
        result_lines.append(" | ".join(header))
        result_lines.append("-" * 80)

        for row in response.get("rows", []):
            data = [v[:100] for v in row.get("keys", [])]
            data.append(str(row.get("clicks", 0)))
            data.append(str(row.get("impressions", 0)))
            data.append(f"{row.get('ctr', 0) * 100:.2f}%")
            data.append(f"{row.get('position', 0):.1f}")
            result_lines.append(" | ".join(data))

        return "\n".join(result_lines)
    except Exception as e:
        if "404" in str(e):
            return _site_not_found_error(site_url)
        return f"Error retrieving search analytics: {str(e)}"


@mcp.tool()
async def get_advanced_search_analytics(
    site_url: str,
    start_date: str = None,
    end_date: str = None,
    dimensions: str = "query",
    search_type: str = "WEB",
    row_limit: int = 1000,
    start_row: int = 0,
    sort_by: str = "clicks",
    sort_direction: str = "descending",
    filter_dimension: str = None,
    filter_operator: str = "contains",
    filter_expression: str = None,
    filters: str = None,
    data_state: str = None,
) -> str:
    """
    Get advanced search analytics with sorting, filtering (including regex), and pagination.

    Args:
        site_url: Exact GSC property URL (e.g. "sc-domain:example.com")
        start_date: Start date YYYY-MM-DD (defaults to 28 days ago)
        end_date: End date YYYY-MM-DD (defaults to today)
        dimensions: Dimensions comma-separated (query,page,device,country,date,searchAppearance)
        search_type: WEB, IMAGE, VIDEO, NEWS, DISCOVER
        row_limit: Max rows (up to 25000)
        start_row: Starting row for pagination
        sort_by: Metric to sort by (clicks, impressions, ctr, position)
        sort_direction: ascending or descending
        filter_dimension: Single filter dimension (query, page, country, device)
        filter_operator: contains, equals, notContains, notEquals, includingRegex, excludingRegex
        filter_expression: Filter value
        filters: JSON array of filter objects for AND logic. Each needs dimension, operator, expression.
        data_state: "all" (default) or "final" (confirmed only, 2-3 day lag)
    """
    try:
        service = get_gsc_service()

        if not end_date:
            end_date = datetime.now().date().strftime("%Y-%m-%d")
        if not start_date:
            start_date = (datetime.now().date() - timedelta(days=28)).strftime("%Y-%m-%d")

        resolved_data_state = (data_state or DATA_STATE).lower().strip()
        if resolved_data_state not in ("all", "final"):
            return f"Invalid data_state '{data_state}'. Use 'all' or 'final'."

        dimension_list = [d.strip() for d in dimensions.split(",")]

        request = {
            "startDate": start_date,
            "endDate": end_date,
            "dimensions": dimension_list,
            "rowLimit": min(row_limit, 25000),
            "startRow": start_row,
            "searchType": search_type.upper(),
            "dataState": resolved_data_state,
        }

        metric_map = {"clicks": "CLICK_COUNT", "impressions": "IMPRESSION_COUNT", "ctr": "CTR", "position": "POSITION"}
        direction_map = {"ascending": "ASCENDING", "descending": "DESCENDING"}
        if sort_by in metric_map:
            resolved_direction = direction_map.get(sort_direction.lower(), sort_direction.upper())
            request["orderBy"] = [{"metric": metric_map[sort_by], "direction": resolved_direction}]

        active_filters = []
        if filters:
            try:
                filter_list = json.loads(filters)
            except json.JSONDecodeError:
                return "Invalid filters JSON."
            if not isinstance(filter_list, list) or not filter_list:
                return "Expected a non-empty JSON array of filter objects."
            for f in filter_list:
                if not all(k in f for k in ("dimension", "operator", "expression")):
                    return f"Each filter must have dimension, operator, expression. Invalid: {f}"
            request["dimensionFilterGroups"] = [{"filters": filter_list}]
            active_filters = filter_list
        elif filter_dimension and filter_expression:
            single = {"dimension": filter_dimension, "operator": filter_operator, "expression": filter_expression}
            request["dimensionFilterGroups"] = [{"filters": [single]}]
            active_filters = [single]

        response = service.searchanalytics().query(siteUrl=site_url, body=request).execute()

        if not response.get("rows"):
            return f"No data found for {site_url} with the specified parameters."

        result_lines = [f"Search analytics for {site_url} ({start_date} to {end_date}, type={search_type}):"]
        if active_filters:
            filter_desc = " AND ".join(f"{f['dimension']} {f['operator']} '{f['expression']}'" for f in active_filters)
            result_lines.append(f"Filters: {filter_desc}")
        result_lines.append(f"Rows {start_row + 1} to {start_row + len(response['rows'])} (sorted by {sort_by} {sort_direction})")
        result_lines.append("-" * 80)

        header = [d.capitalize() for d in dimension_list] + ["Clicks", "Impressions", "CTR", "Position"]
        result_lines.append(" | ".join(header))
        result_lines.append("-" * 80)

        for row in response["rows"]:
            data = [v[:100] for v in row.get("keys", [])]
            data.append(str(row.get("clicks", 0)))
            data.append(str(row.get("impressions", 0)))
            data.append(f"{row.get('ctr', 0) * 100:.2f}%")
            data.append(f"{row.get('position', 0):.1f}")
            result_lines.append(" | ".join(data))

        if len(response["rows"]) == row_limit:
            result_lines.append(f"\nMore results available. Use start_row: {start_row + row_limit}")

        return "\n".join(result_lines)
    except Exception as e:
        if "404" in str(e):
            return _site_not_found_error(site_url)
        return f"Error: {str(e)}"


@mcp.tool()
async def get_performance_overview(site_url: str, days: int = 28) -> str:
    """
    Get a performance overview with totals and daily trend.

    Args:
        site_url: Exact GSC property URL
        days: Number of days to look back (default: 28)
    """
    try:
        service = get_gsc_service()
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=days)
        date_range = {"startDate": start_date.strftime("%Y-%m-%d"), "endDate": end_date.strftime("%Y-%m-%d")}

        total_response = service.searchanalytics().query(
            siteUrl=site_url,
            body={**date_range, "dimensions": [], "rowLimit": 1, "dataState": DATA_STATE},
        ).execute()

        date_response = service.searchanalytics().query(
            siteUrl=site_url,
            body={**date_range, "dimensions": ["date"], "rowLimit": days, "dataState": DATA_STATE},
        ).execute()

        result_lines = [f"Performance Overview for {site_url} (last {days} days):", "-" * 80]

        if total_response.get("rows"):
            row = total_response["rows"][0]
            result_lines.append(f"Total Clicks: {row.get('clicks', 0):,}")
            result_lines.append(f"Total Impressions: {row.get('impressions', 0):,}")
            result_lines.append(f"Average CTR: {row.get('ctr', 0) * 100:.2f}%")
            result_lines.append(f"Average Position: {row.get('position', 0):.1f}")
        else:
            return "No data available for the selected period."

        if date_response.get("rows"):
            result_lines.append("\nDaily Trend:")
            result_lines.append("Date | Clicks | Impressions | CTR | Position")
            result_lines.append("-" * 60)
            for row in sorted(date_response["rows"], key=lambda x: x["keys"][0]):
                d = row["keys"][0]
                result_lines.append(
                    f"{d} | {row.get('clicks', 0)} | {row.get('impressions', 0)} | "
                    f"{row.get('ctr', 0) * 100:.2f}% | {row.get('position', 0):.1f}"
                )

        return "\n".join(result_lines)
    except Exception as e:
        if "404" in str(e):
            return _site_not_found_error(site_url)
        return f"Error: {str(e)}"


@mcp.tool()
async def compare_search_periods(
    site_url: str,
    period1_start: str,
    period1_end: str,
    period2_start: str,
    period2_end: str,
    dimensions: str = "query",
    limit: int = 20,
) -> str:
    """
    Compare search analytics between two time periods.

    Args:
        site_url: Exact GSC property URL
        period1_start: Start date for period 1 (YYYY-MM-DD)
        period1_end: End date for period 1
        period2_start: Start date for period 2
        period2_end: End date for period 2
        dimensions: Dimensions to group by (default: query)
        limit: Top N results to compare (default: 20)
    """
    try:
        service = get_gsc_service()
        dimension_list = [d.strip() for d in dimensions.split(",")]

        base = {"dimensions": dimension_list, "rowLimit": 1000, "dataState": DATA_STATE}
        p1 = service.searchanalytics().query(
            siteUrl=site_url, body={**base, "startDate": period1_start, "endDate": period1_end}
        ).execute()
        p2 = service.searchanalytics().query(
            siteUrl=site_url, body={**base, "startDate": period2_start, "endDate": period2_end}
        ).execute()

        p1_data = {tuple(r["keys"]): r for r in p1.get("rows", [])}
        p2_data = {tuple(r["keys"]): r for r in p2.get("rows", [])}
        all_keys = set(p1_data) | set(p2_data)

        comparisons = []
        for key in all_keys:
            r1 = p1_data.get(key, {"clicks": 0, "impressions": 0, "ctr": 0, "position": 0})
            r2 = p2_data.get(key, {"clicks": 0, "impressions": 0, "ctr": 0, "position": 0})
            click_diff = r2.get("clicks", 0) - r1.get("clicks", 0)
            pos_diff = r1.get("position", 0) - r2.get("position", 0)  # positive = improved
            comparisons.append({"key": key, "p1_clicks": r1.get("clicks", 0), "p2_clicks": r2.get("clicks", 0),
                                "click_diff": click_diff, "p1_pos": r1.get("position", 0),
                                "p2_pos": r2.get("position", 0), "pos_diff": pos_diff})

        comparisons.sort(key=lambda x: abs(x["click_diff"]), reverse=True)

        result_lines = [
            f"Comparison for {site_url}:",
            f"Period 1: {period1_start} to {period1_end}",
            f"Period 2: {period2_start} to {period2_end}",
            "-" * 100,
            f"{' | '.join(d.capitalize() for d in dimension_list)} | P1 Clicks | P2 Clicks | Change | P1 Pos | P2 Pos | Pos Change",
            "-" * 100,
        ]

        for item in comparisons[:limit]:
            key_str = " | ".join(str(k)[:80] for k in item["key"])
            result_lines.append(
                f"{key_str} | {item['p1_clicks']} | {item['p2_clicks']} | {item['click_diff']:+d} | "
                f"{item['p1_pos']:.1f} | {item['p2_pos']:.1f} | {item['pos_diff']:+.1f}"
            )

        return "\n".join(result_lines)
    except Exception as e:
        if "404" in str(e):
            return _site_not_found_error(site_url)
        return f"Error: {str(e)}"


@mcp.tool()
async def get_search_by_page_query(site_url: str, page_url: str, days: int = 28, row_limit: int = 20) -> str:
    """
    Get search queries driving traffic to a specific page.

    Args:
        site_url: Exact GSC property URL
        page_url: The specific page URL to analyze
        days: Days to look back (default: 28)
        row_limit: Rows to return (default: 20, max: 500)
    """
    try:
        service = get_gsc_service()
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=days)

        response = service.searchanalytics().query(
            siteUrl=site_url,
            body={
                "startDate": start_date.strftime("%Y-%m-%d"),
                "endDate": end_date.strftime("%Y-%m-%d"),
                "dimensions": ["query"],
                "dimensionFilterGroups": [{"filters": [{"dimension": "page", "operator": "equals", "expression": page_url}]}],
                "rowLimit": min(max(1, row_limit), 500),
                "orderBy": [{"metric": "CLICK_COUNT", "direction": "descending"}],
                "dataState": DATA_STATE,
            },
        ).execute()

        if not response.get("rows"):
            return f"No search data found for {page_url} in the last {days} days."

        result_lines = [f"Queries for {page_url} (last {days} days):", "-" * 80,
                        "Query | Clicks | Impressions | CTR | Position", "-" * 80]

        for row in response["rows"]:
            q = row["keys"][0][:100]
            result_lines.append(
                f"{q} | {row.get('clicks', 0)} | {row.get('impressions', 0)} | "
                f"{row.get('ctr', 0) * 100:.2f}% | {row.get('position', 0):.1f}"
            )

        total_clicks = sum(r.get("clicks", 0) for r in response["rows"])
        total_imp = sum(r.get("impressions", 0) for r in response["rows"])
        result_lines.append("-" * 80)
        result_lines.append(f"TOTAL | {total_clicks} | {total_imp} | {(total_clicks / total_imp * 100) if total_imp else 0:.2f}%")

        return "\n".join(result_lines)
    except Exception as e:
        return f"Error: {str(e)}"


# ──────────────────────────────────────────────────────────────────────────────
# URL Inspection Tools
# ──────────────────────────────────────────────────────────────────────────────

@mcp.tool()
async def inspect_url(site_url: str, page_url: str) -> str:
    """
    Inspect a URL for indexing status, rich results, and mobile usability.

    Args:
        site_url: Exact GSC property URL (e.g. "sc-domain:example.com")
        page_url: The specific URL to inspect
    """
    try:
        service = get_gsc_service()
        response = service.urlInspection().index().inspect(
            body={"inspectionUrl": page_url, "siteUrl": site_url}
        ).execute()

        if not response or "inspectionResult" not in response:
            return f"No inspection data found for {page_url}."

        inspection = response["inspectionResult"]
        index_status = inspection.get("indexStatusResult", {})

        result_lines = [f"URL Inspection for {page_url}:", "-" * 80]

        if "inspectionResultLink" in inspection:
            result_lines.append(f"GSC Link: {inspection['inspectionResultLink']}")

        result_lines.append(f"Verdict: {index_status.get('verdict', 'UNKNOWN')}")
        if "coverageState" in index_status:
            result_lines.append(f"Coverage: {index_status['coverageState']}")
        if "lastCrawlTime" in index_status:
            result_lines.append(f"Last Crawled: {index_status['lastCrawlTime'][:10]}")
        if "pageFetchState" in index_status:
            result_lines.append(f"Page Fetch: {index_status['pageFetchState']}")
        if "robotsTxtState" in index_status:
            result_lines.append(f"Robots.txt: {index_status['robotsTxtState']}")
        if "indexingState" in index_status:
            result_lines.append(f"Indexing: {index_status['indexingState']}")
        if "googleCanonical" in index_status:
            result_lines.append(f"Google Canonical: {index_status['googleCanonical']}")
        if "userCanonical" in index_status and index_status.get("userCanonical") != index_status.get("googleCanonical"):
            result_lines.append(f"User Canonical: {index_status['userCanonical']}")
        if "crawledAs" in index_status:
            result_lines.append(f"Crawled As: {index_status['crawledAs']}")

        referring = index_status.get("referringUrls", [])
        if referring:
            result_lines.append(f"\nReferring URLs ({len(referring)}):")
            for url in referring[:5]:
                result_lines.append(f"  - {url}")

        rich = inspection.get("richResultsResult", {})
        if rich:
            result_lines.append(f"\nRich Results: {rich.get('verdict', 'UNKNOWN')}")
            for item in rich.get("detectedItems", []):
                result_lines.append(f"  - {item.get('richResultType', 'Unknown')}")

        mobile = inspection.get("mobileUsabilityResult", {})
        if mobile and mobile.get("verdict") != "VERDICT_UNSPECIFIED":
            result_lines.append(f"\nMobile Usability: {mobile.get('verdict', 'UNKNOWN')}")

        return "\n".join(result_lines)
    except Exception as e:
        if "404" in str(e):
            return _site_not_found_error(site_url)
        return f"Error inspecting URL: {str(e)}"


@mcp.tool()
async def batch_inspect_urls(site_url: str, urls: str) -> str:
    """
    Inspect multiple URLs for indexing status. Handles rate limiting automatically.
    API limit: 2000/day, 600/minute. This tool handles up to 50 URLs per call.

    Args:
        site_url: Exact GSC property URL (e.g. "sc-domain:example.com")
        urls: List of URLs to inspect, one per line
    """
    try:
        service = get_gsc_service()
        url_list = [u.strip() for u in urls.split("\n") if u.strip()]

        if not url_list:
            return "No URLs provided."
        if len(url_list) > 50:
            return f"Too many URLs ({len(url_list)}). Limit to 50 per batch."

        categories = {"indexed": [], "crawled_not_indexed": [], "not_found": [],
                      "blocked": [], "unknown": [], "error": []}
        details = []

        for i, page_url in enumerate(url_list):
            try:
                response = service.urlInspection().index().inspect(
                    body={"inspectionUrl": page_url, "siteUrl": site_url}
                ).execute()

                idx = response.get("inspectionResult", {}).get("indexStatusResult", {})
                verdict = idx.get("verdict", "UNKNOWN")
                coverage = idx.get("coverageState", "Unknown")
                crawled = idx.get("lastCrawlTime", "never")
                crawl_date = crawled[:10] if crawled != "never" else "never"
                canonical = idx.get("googleCanonical", "")

                short = page_url.split("//", 1)[-1] if "//" in page_url else page_url
                details.append(f"{short} | {verdict} | {coverage} | crawled: {crawl_date}")

                if verdict == "PASS":
                    categories["indexed"].append(page_url)
                elif "not indexed" in coverage.lower():
                    categories["crawled_not_indexed"].append(page_url)
                elif "not found" in coverage.lower() or "404" in coverage.lower():
                    categories["not_found"].append(page_url)
                elif idx.get("robotsTxtState") == "BLOCKED":
                    categories["blocked"].append(page_url)
                else:
                    categories["unknown"].append(f"{page_url} ({coverage})")

                # Rate limiting: 600/min = 10/sec, be conservative
                if i < len(url_list) - 1:
                    await asyncio.sleep(0.15)

            except Exception as e:
                categories["error"].append(f"{page_url}: {str(e)[:80]}")

        result_lines = [f"Batch Inspection for {site_url} ({len(url_list)} URLs):", "-" * 80]
        result_lines.append(f"Indexed: {len(categories['indexed'])}")
        result_lines.append(f"Crawled not indexed: {len(categories['crawled_not_indexed'])}")
        result_lines.append(f"Not found (404): {len(categories['not_found'])}")
        result_lines.append(f"Blocked: {len(categories['blocked'])}")
        result_lines.append(f"Unknown/Other: {len(categories['unknown'])}")
        result_lines.append(f"Errors: {len(categories['error'])}")
        result_lines.append("-" * 80)
        result_lines.append("\nDetailed results:")
        result_lines.extend(details)

        for cat_name, cat_list in categories.items():
            if cat_list and cat_name not in ("indexed",):
                result_lines.append(f"\n{cat_name.upper().replace('_', ' ')}:")
                for item in cat_list:
                    result_lines.append(f"  - {item}")

        return "\n".join(result_lines)
    except Exception as e:
        return f"Error: {str(e)}"


# ──────────────────────────────────────────────────────────────────────────────
# Sitemap Tools
# ──────────────────────────────────────────────────────────────────────────────

@mcp.tool()
async def get_sitemaps(site_url: str) -> str:
    """
    List all sitemaps for a property with detailed info.

    Args:
        site_url: Exact GSC property URL
    """
    try:
        service = get_gsc_service()
        sitemaps = service.sitemaps().list(siteUrl=site_url).execute()

        if not sitemaps.get("sitemap"):
            return f"No sitemaps found for {site_url}."

        result_lines = [f"Sitemaps for {site_url}:", "-" * 100,
                        "Path | Last Downloaded | Type | URLs | Errors | Warnings", "-" * 100]

        for sm in sitemaps["sitemap"]:
            path = sm.get("path", "Unknown")
            last_dl = sm.get("lastDownloaded", "Never")
            if last_dl != "Never":
                try:
                    last_dl = datetime.fromisoformat(last_dl.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
                except Exception:
                    pass

            sm_type = "Index" if sm.get("isSitemapsIndex", False) else "Sitemap"
            errors = int(sm.get("errors", 0))
            warnings = int(sm.get("warnings", 0))

            url_count = "N/A"
            for c in sm.get("contents", []):
                if c.get("type") == "web":
                    url_count = c.get("submitted", "0")
                    break

            result_lines.append(f"{path} | {last_dl} | {sm_type} | {url_count} | {errors} | {warnings}")

        return "\n".join(result_lines)
    except Exception as e:
        if "404" in str(e):
            return _site_not_found_error(site_url)
        return f"Error: {str(e)}"


@mcp.tool()
async def submit_sitemap(site_url: str, sitemap_url: str) -> str:
    """
    Submit or resubmit a sitemap to Google.

    Args:
        site_url: Exact GSC property URL
        sitemap_url: Full URL of the sitemap to submit
    """
    try:
        service = get_gsc_service()
        service.sitemaps().submit(siteUrl=site_url, feedpath=sitemap_url).execute()
        return f"Successfully submitted sitemap: {sitemap_url}\nGoogle will queue it for processing."
    except Exception as e:
        return f"Error submitting sitemap: {str(e)}"


@mcp.tool()
async def delete_sitemap(site_url: str, sitemap_url: str) -> str:
    """
    Delete (unsubmit) a sitemap from Google Search Console.

    Args:
        site_url: Exact GSC property URL
        sitemap_url: Full URL of the sitemap to delete
    """
    try:
        service = get_gsc_service()
        service.sitemaps().delete(siteUrl=site_url, feedpath=sitemap_url).execute()
        return f"Deleted sitemap: {sitemap_url}\nAlready-indexed URLs will remain in Google's index."
    except Exception as e:
        return f"Error deleting sitemap: {str(e)}"


# ──────────────────────────────────────────────────────────────────────────────
# NEW: Google Indexing API Tools
# ──────────────────────────────────────────────────────────────────────────────

@mcp.tool()
async def request_indexing(url: str) -> str:
    """
    Request Google to crawl and index a URL via the Indexing API.
    IMPORTANT: Only works for pages with JobPosting or BroadcastEvent structured data.
    Default quota: 200 requests/day.

    Args:
        url: The full URL to request indexing for
    """
    try:
        service = get_indexing_service()
        response = service.urlNotifications().publish(
            body={"url": url, "type": "URL_UPDATED"}
        ).execute()

        notify_time = response.get("urlNotificationMetadata", {}).get("latestUpdate", {}).get("notifyTime", "unknown")
        return f"Indexing requested for: {url}\nNotification time: {notify_time}\nGoogle will crawl this URL soon."
    except HttpError as e:
        if e.resp.status == 429:
            return f"Rate limit exceeded. You've hit the daily quota (default 200/day). Try again tomorrow."
        elif e.resp.status == 403:
            return (
                f"Permission denied for Indexing API. Ensure:\n"
                f"1. The Indexing API is enabled in Google Cloud Console\n"
                f"2. Your service account has 'Owner' permission in GSC for this site\n"
                f"3. The page has JobPosting or BroadcastEvent structured data"
            )
        return f"Error (HTTP {e.resp.status}): {str(e)}"
    except Exception as e:
        return f"Error requesting indexing: {str(e)}"


@mcp.tool()
async def request_removal(url: str) -> str:
    """
    Request Google to remove a URL from the index via the Indexing API.
    IMPORTANT: Only works for pages with JobPosting or BroadcastEvent structured data.

    Args:
        url: The full URL to request removal for
    """
    try:
        service = get_indexing_service()
        response = service.urlNotifications().publish(
            body={"url": url, "type": "URL_DELETED"}
        ).execute()

        return f"Removal requested for: {url}\nGoogle will process this request."
    except HttpError as e:
        if e.resp.status == 429:
            return "Rate limit exceeded. Try again tomorrow."
        elif e.resp.status == 403:
            return "Permission denied. Ensure the Indexing API is enabled and you have Owner permission."
        return f"Error (HTTP {e.resp.status}): {str(e)}"
    except Exception as e:
        return f"Error requesting removal: {str(e)}"


@mcp.tool()
async def batch_request_indexing(urls: str) -> str:
    """
    Request indexing for multiple URLs. Processes sequentially with rate limiting.
    Default quota: 200/day. Only for pages with JobPosting or BroadcastEvent structured data.

    Args:
        urls: List of URLs to index, one per line (max 100 per batch)
    """
    try:
        service = get_indexing_service()
        url_list = [u.strip() for u in urls.split("\n") if u.strip()]

        if not url_list:
            return "No URLs provided."
        if len(url_list) > 100:
            return f"Too many URLs ({len(url_list)}). Max 100 per batch (API quota is 200/day)."

        results = {"success": [], "failed": []}

        for url in url_list:
            try:
                service.urlNotifications().publish(
                    body={"url": url, "type": "URL_UPDATED"}
                ).execute()
                results["success"].append(url)
                await asyncio.sleep(0.5)  # Rate limiting
            except HttpError as e:
                if e.resp.status == 429:
                    results["failed"].append(f"{url}: Rate limit exceeded")
                    break  # Stop on rate limit
                results["failed"].append(f"{url}: HTTP {e.resp.status}")
            except Exception as e:
                results["failed"].append(f"{url}: {str(e)[:60]}")

        lines = [f"Batch Indexing Results:", "-" * 60,
                 f"Submitted: {len(results['success'])}", f"Failed: {len(results['failed'])}"]

        if results["failed"]:
            lines.append("\nFailed URLs:")
            for f in results["failed"]:
                lines.append(f"  - {f}")

        lines.append(f"\nRemaining daily quota: ~{200 - len(results['success'])}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {str(e)}"


@mcp.tool()
async def check_indexing_notification(url: str) -> str:
    """
    Check the latest indexing notification status for a URL.

    Args:
        url: The URL to check
    """
    try:
        service = get_indexing_service()
        response = service.urlNotifications().getMetadata(url=url).execute()

        lines = [f"Indexing notification status for: {url}", "-" * 60]

        latest_update = response.get("latestUpdate", {})
        if latest_update:
            lines.append(f"Latest update type: {latest_update.get('type', 'unknown')}")
            lines.append(f"Notify time: {latest_update.get('notifyTime', 'unknown')}")
            lines.append(f"URL: {latest_update.get('url', 'unknown')}")

        latest_remove = response.get("latestRemove", {})
        if latest_remove:
            lines.append(f"\nLatest removal type: {latest_remove.get('type', 'unknown')}")
            lines.append(f"Notify time: {latest_remove.get('notifyTime', 'unknown')}")

        return "\n".join(lines)
    except HttpError as e:
        if e.resp.status == 404:
            return f"No indexing notifications found for {url}. This URL hasn't been submitted via the Indexing API."
        return f"Error (HTTP {e.resp.status}): {str(e)}"
    except Exception as e:
        return f"Error: {str(e)}"


# ──────────────────────────────────────────────────────────────────────────────
# NEW: Core Web Vitals (CrUX API)
# ──────────────────────────────────────────────────────────────────────────────

def _format_crux_metric(metric_data: dict, name: str) -> str:
    """Format a single CrUX metric."""
    if not metric_data:
        return f"  {name}: No data"

    percentiles = metric_data.get("percentiles", {})
    p75 = percentiles.get("p75")

    histogram = metric_data.get("histogram", [])
    good = histogram[0].get("density", 0) * 100 if len(histogram) > 0 else 0
    needs_improvement = histogram[1].get("density", 0) * 100 if len(histogram) > 1 else 0
    poor = histogram[2].get("density", 0) * 100 if len(histogram) > 2 else 0

    return f"  {name}: p75={p75} | Good: {good:.0f}% | Needs Improvement: {needs_improvement:.0f}% | Poor: {poor:.0f}%"


@mcp.tool()
async def get_core_web_vitals(url_or_origin: str, form_factor: str = "PHONE") -> str:
    """
    Get Core Web Vitals (LCP, INP, CLS) from the Chrome UX Report (CrUX) API.
    Free API, no OAuth needed — just a CRUX_API_KEY env variable.

    Args:
        url_or_origin: Full URL or origin (e.g. "https://example.com" for origin-level)
        form_factor: PHONE, DESKTOP, or TABLET (default: PHONE)
    """
    if not CRUX_API_KEY:
        return (
            "CrUX API key not configured. Set the CRUX_API_KEY environment variable.\n"
            "Get a free key at: https://console.cloud.google.com/apis/credentials\n"
            "Enable the 'Chrome UX Report API' in your Google Cloud project."
        )

    import urllib.request
    import urllib.error

    # Determine if it's a specific URL or an origin (no path beyond /)
    body = {"formFactor": form_factor.upper()}
    parsed = urlparse(url_or_origin)
    has_path = parsed.path not in ("", "/")
    if has_path:
        body["url"] = url_or_origin
    else:
        body["origin"] = url_or_origin.rstrip("/")

    try:
        req = urllib.request.Request(
            f"https://chromeuxreport.googleapis.com/v1/records:queryRecord?key={CRUX_API_KEY}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())

        record = data.get("record", {})
        metrics = record.get("metrics", {})
        key = record.get("key", {})

        lines = [f"Core Web Vitals for {key.get('url') or key.get('origin', url_or_origin)}:"]
        lines.append(f"Form factor: {key.get('formFactor', form_factor)}")
        lines.append(f"Collection period: {data.get('record', {}).get('collectionPeriod', {}).get('firstDate', {}).get('year', '?')}-{data.get('record', {}).get('collectionPeriod', {}).get('firstDate', {}).get('month', '?')} to {data.get('record', {}).get('collectionPeriod', {}).get('lastDate', {}).get('year', '?')}-{data.get('record', {}).get('collectionPeriod', {}).get('lastDate', {}).get('month', '?')}")
        lines.append("-" * 60)

        lines.append(_format_crux_metric(metrics.get("largest_contentful_paint"), "LCP (Largest Contentful Paint)"))
        lines.append(_format_crux_metric(metrics.get("interaction_to_next_paint"), "INP (Interaction to Next Paint)"))
        lines.append(_format_crux_metric(metrics.get("cumulative_layout_shift"), "CLS (Cumulative Layout Shift)"))
        lines.append(_format_crux_metric(metrics.get("first_contentful_paint"), "FCP (First Contentful Paint)"))
        lines.append(_format_crux_metric(metrics.get("experimental_time_to_first_byte"), "TTFB (Time to First Byte)"))

        # Overall assessment
        lcp_p75 = metrics.get("largest_contentful_paint", {}).get("percentiles", {}).get("p75", 99999)
        inp_p75 = metrics.get("interaction_to_next_paint", {}).get("percentiles", {}).get("p75", 99999)
        cls_p75 = metrics.get("cumulative_layout_shift", {}).get("percentiles", {}).get("p75", 99999)

        lines.append("\n--- Assessment ---")
        lcp_ok = lcp_p75 <= 2500 if isinstance(lcp_p75, (int, float)) else False
        inp_ok = inp_p75 <= 200 if isinstance(inp_p75, (int, float)) else False
        cls_ok = cls_p75 <= 0.1 if isinstance(cls_p75, (int, float)) else False

        lines.append(f"LCP: {'GOOD' if lcp_ok else 'NEEDS WORK'} (threshold: 2500ms)")
        lines.append(f"INP: {'GOOD' if inp_ok else 'NEEDS WORK'} (threshold: 200ms)")
        lines.append(f"CLS: {'GOOD' if cls_ok else 'NEEDS WORK'} (threshold: 0.1)")

        if lcp_ok and inp_ok and cls_ok:
            lines.append("\nOverall: PASSING Core Web Vitals")
        else:
            lines.append("\nOverall: FAILING Core Web Vitals")

        return "\n".join(lines)

    except urllib.error.HTTPError as e:
        if e.code == 404:
            return f"No CrUX data available for {url_or_origin}. The site may not have enough traffic for Chrome to collect data."
        error_body = e.read().decode() if hasattr(e, 'read') else str(e)
        # Strip API key from error messages to avoid leaking it
        safe_body = error_body.replace(CRUX_API_KEY, "[REDACTED]") if CRUX_API_KEY else error_body
        return f"CrUX API error (HTTP {e.code}): {safe_body[:200]}"
    except Exception as e:
        return f"Error fetching Core Web Vitals: {str(e)}"


# ──────────────────────────────────────────────────────────────────────────────
# NEW: SEO Analysis Tools
# ──────────────────────────────────────────────────────────────────────────────

@mcp.tool()
async def get_pagespeed_insights(
    url: str,
    strategy: str = "mobile",
    categories: str = "performance,seo,accessibility,best-practices",
) -> str:
    """
    Run Google's PageSpeed Insights API for a URL.
    Returns Lighthouse lab data plus available Chrome UX Report field data.

    Args:
        url: Full page URL
        strategy: mobile or desktop
        categories: Comma-separated Lighthouse categories
    """
    normalized_url = _ensure_https_url(url)
    strategy_value = strategy.lower().strip()
    if strategy_value not in {"mobile", "desktop"}:
        return "Invalid strategy. Use 'mobile' or 'desktop'."

    selected_categories = []
    for raw in categories.split(","):
        category = raw.strip().lower()
        if not category:
            continue
        if category not in LIGHTHOUSE_CATEGORIES:
            return f"Invalid Lighthouse category '{category}'. Allowed: {', '.join(sorted(LIGHTHOUSE_CATEGORIES))}"
        selected_categories.append(category)
    if not selected_categories:
        selected_categories = ["performance", "seo"]

    params = [("url", normalized_url), ("strategy", strategy_value)]
    for category in selected_categories:
        params.append(("category", category))
    if PAGESPEED_API_KEY:
        params.append(("key", PAGESPEED_API_KEY))

    try:
        async with httpx.AsyncClient(timeout=DEFAULT_FETCH_TIMEOUT, headers=DEFAULT_FETCH_HEADERS) as client:
            response = await client.get(
                "https://www.googleapis.com/pagespeedonline/v5/runPagespeed",
                params=params,
            )
        response.raise_for_status()
        payload = response.json()

        lines = [f"PageSpeed Insights for {normalized_url} ({strategy_value})"]
        lines.append(_summarize_lighthouse_payload(payload, "Lighthouse / PSI summary"))
        lines.extend(_format_loading_experience(payload.get("loadingExperience", {}), "URL field data"))
        origin_data = payload.get("originLoadingExperience", {})
        if origin_data:
            lines.extend(_format_loading_experience(origin_data, "Origin field data"))
        return "\n".join(lines)
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:300]
        safe_body = body.replace(PAGESPEED_API_KEY, "[REDACTED]") if PAGESPEED_API_KEY else body
        if exc.response.status_code == 429:
            guidance = (
                " PageSpeed quota was exceeded. "
                "Set PAGESPEED_API_KEY (or GOOGLE_API_KEY) to use your own Google API quota."
            )
        else:
            guidance = ""
        return f"PageSpeed Insights error (HTTP {exc.response.status_code}): {safe_body}{guidance}"
    except Exception as exc:
        return f"Error running PageSpeed Insights: {str(exc)}"


@mcp.tool()
async def run_lighthouse_audit(
    url: str,
    form_factor: str = "mobile",
    categories: str = "performance,seo,accessibility,best-practices",
) -> str:
    """
    Run a local Lighthouse CLI audit via npx.
    Requires Node.js plus a locally available Chrome/Chromium browser.

    Args:
        url: Full page URL
        form_factor: mobile or desktop
        categories: Comma-separated Lighthouse categories
    """
    normalized_url = _ensure_https_url(url)
    strategy_value = form_factor.lower().strip()
    if strategy_value not in {"mobile", "desktop"}:
        return "Invalid form_factor. Use 'mobile' or 'desktop'."

    selected_categories = []
    for raw in categories.split(","):
        category = raw.strip().lower()
        if not category:
            continue
        if category not in LIGHTHOUSE_CATEGORIES:
            return f"Invalid Lighthouse category '{category}'. Allowed: {', '.join(sorted(LIGHTHOUSE_CATEGORIES))}"
        selected_categories.append(category)
    if not selected_categories:
        selected_categories = ["performance", "seo"]

    npx_path = shutil.which("npx")
    if not npx_path:
        return "npx is not available. Install Node.js to use the local Lighthouse audit."

    command = [
        npx_path,
        "--yes",
        "lighthouse",
        normalized_url,
        "--output=json",
        "--output-path=stdout",
        "--quiet",
        f"--only-categories={','.join(selected_categories)}",
        "--chrome-flags=--headless=new --no-sandbox --disable-gpu",
    ]
    if strategy_value == "desktop":
        command.append("--preset=desktop")
    if LIGHTHOUSE_CHROME_PATH:
        command.append(f"--chrome-path={LIGHTHOUSE_CHROME_PATH}")

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            if not stderr and result.stdout:
                stderr = result.stdout.strip()
            return (
                "Local Lighthouse failed. "
                f"Exit code: {result.returncode}. "
                f"Details: {_clip(stderr or 'No stderr output', 400)}"
            )

        payload = json.loads(result.stdout)
        return _summarize_lighthouse_payload(payload, f"Local Lighthouse audit for {normalized_url} ({strategy_value})")
    except subprocess.TimeoutExpired:
        return "Local Lighthouse timed out after 180 seconds."
    except json.JSONDecodeError:
        return "Local Lighthouse returned invalid JSON output."
    except Exception as exc:
        return f"Error running local Lighthouse: {str(exc)}"


@mcp.tool()
async def inspect_robots_txt(url_or_origin: str) -> str:
    """
    Fetch and summarize the site's robots.txt file.

    Args:
        url_or_origin: Full URL, origin, or sc-domain property
    """
    robots_url = _origin_from_url(url_or_origin).rstrip("/") + "/robots.txt"
    try:
        response = await _fetch_url(robots_url)
        if response.status_code == 404:
            return (
                f"robots.txt not found at {robots_url} (HTTP 404).\n"
                "Google treats a missing robots.txt as no crawl restrictions."
            )

        text = response.text
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        sitemap_urls = []
        wildcard_disallows = []
        wildcard_allows = []
        warnings = []
        applies_to_wildcard = False

        for raw_line in lines:
            line = raw_line.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            key, value = line.split(":", 1)
            key = key.strip().lower()
            value = value.strip()
            if key == "user-agent":
                applies_to_wildcard = value.lower() == "*"
            elif key == "disallow" and applies_to_wildcard:
                wildcard_disallows.append(value)
            elif key == "allow" and applies_to_wildcard:
                wildcard_allows.append(value)
            elif key == "sitemap":
                sitemap_urls.append(value)
            elif key == "noindex":
                warnings.append("Found noindex directive in robots.txt. Google no longer supports this.")

        blocked_all = any(path == "/" for path in wildcard_disallows)
        output = [
            f"robots.txt inspection for {robots_url}",
            f"HTTP status: {response.status_code}",
            f"User-agent * disallow rules: {len(wildcard_disallows)}",
            f"User-agent * allow rules: {len(wildcard_allows)}",
            f"Blocks all crawling for user-agent *: {'YES' if blocked_all else 'NO'}",
        ]

        if sitemap_urls:
            output.append("Declared sitemap URLs:")
            for item in sitemap_urls[:10]:
                output.append(f"  {item}")
        else:
            output.append("No sitemap directive found in robots.txt.")

        if wildcard_disallows:
            output.append("Sample disallow rules for user-agent *:")
            for item in wildcard_disallows[:10]:
                output.append(f"  {item or '[blank]'}")

        if warnings:
            output.append("Warnings:")
            for warning in warnings:
                output.append(f"  {warning}")

        return "\n".join(output)
    except Exception as exc:
        return f"Error inspecting robots.txt: {str(exc)}"


@mcp.tool()
async def analyze_sitemap(sitemap_url: str, sample_urls: int = 5) -> str:
    """
    Fetch and analyze an XML sitemap or sitemap index.

    Args:
        sitemap_url: Full sitemap URL
        sample_urls: Number of sitemap URLs to validate with GET requests
    """
    normalized_url = _ensure_https_url(sitemap_url)
    try:
        response = await _fetch_url(normalized_url, headers={"Accept": "application/xml,text/xml;q=0.9,*/*;q=0.5"})
        response.raise_for_status()
        xml_text = _extract_xml_text(response)
        parsed = _parse_sitemap_document(xml_text)

        lines = [f"Sitemap analysis for {normalized_url}", f"Detected type: {parsed['type']}"]

        if parsed["type"] == "sitemapindex":
            sitemaps = parsed["sitemaps"]
            lines.append(f"Nested sitemaps: {len(sitemaps)}")
            for item in sitemaps[:10]:
                lines.append(f"  {item}")
            return "\n".join(lines)

        urls = parsed["urls"]
        lines.append(f"URLs listed: {len(urls)}")
        lastmod_count = parsed.get("lastmod_count", 0)
        lines.append(f"URLs with lastmod: {lastmod_count}")
        if urls:
            lines.append("Sample URLs:")
            for item in urls[:10]:
                lines.append(f"  {item['loc']}")

        sample_results = []
        for item in urls[: max(0, min(sample_urls, len(urls)))]:
            target = item["loc"]
            try:
                sample_response = await _fetch_url(target, method="GET")
                sample_results.append((target, sample_response.status_code))
            except Exception:
                sample_results.append((target, "ERROR"))

        if sample_results:
            lines.append("Sample URL status checks:")
            for target, status in sample_results:
                lines.append(f"  {target} -> {status}")

        return "\n".join(lines)
    except Exception as exc:
        return f"Error analyzing sitemap: {str(exc)}"


@mcp.tool()
async def analyze_page_seo(url: str) -> str:
    """
    Fetch a page and analyze on-page SEO signals, structured data, and indexability hints.

    Args:
        url: Full page URL
    """
    normalized_url = _ensure_https_url(url)
    try:
        response = await _fetch_url(normalized_url)
        content_type = response.headers.get("content-type", "").lower()
        final_url = str(response.url)
        if "html" not in content_type and "xml" not in content_type and "<html" not in response.text[:500].lower():
            return (
                f"Page SEO analysis for {normalized_url}\n"
                f"Final URL: {final_url}\n"
                f"HTTP status: {response.status_code}\n"
                f"Content-Type: {content_type or 'unknown'}\n"
                "Response is not HTML, so on-page SEO analysis was skipped."
            )

        analysis = _analyze_html_document(final_url, response.status_code, dict(response.headers), response.text)
        lines = [
            f"Page SEO analysis for {normalized_url}",
            f"Final URL: {final_url}",
            f"HTTP status: {response.status_code} ({_status_class(response.status_code)})",
            f"Title: {_clip(analysis['title'] or '[missing]', 120)}",
            f"Meta description: {_clip(analysis['meta_description'] or '[missing]', 180)}",
            f"Canonical: {_clip(analysis['canonicals'][0], 120) if analysis['canonicals'] else '[missing]'}",
            f"H1 count: {len(analysis['headings']['h1'])}",
            f"H2 count: {len(analysis['headings']['h2'])}",
            f"Word count: {analysis['body_word_count']}",
            f"Structured data types: {', '.join(analysis['structured_types']) if analysis['structured_types'] else '[none found]'}",
            f"Hreflang count: {len(analysis['hreflangs'])}",
        ]

        if analysis["issues"]:
            lines.append("Issues:")
            for issue in analysis["issues"]:
                lines.append(f"  {issue}")
        if analysis["notes"]:
            lines.append("Notes:")
            for note in analysis["notes"]:
                lines.append(f"  {note}")
        if analysis["headings"]["h1"]:
            lines.append("H1 text:")
            for heading in analysis["headings"]["h1"][:3]:
                lines.append(f"  {heading}")
        return "\n".join(lines)
    except Exception as exc:
        return f"Error analyzing page SEO: {str(exc)}"


@mcp.tool()
async def crawl_site_seo(start_url: str, max_pages: int = 10) -> str:
    """
    Crawl a site from a start URL and aggregate common technical/on-page SEO issues.

    Args:
        start_url: First URL to crawl
        max_pages: Maximum number of same-origin HTML pages to crawl
    """
    normalized_start = _ensure_https_url(start_url)
    origin = _origin_from_url(normalized_start)
    queue = deque([normalized_start])
    visited: Set[str] = set()
    page_summaries = []
    duplicate_titles: Counter[str] = Counter()
    duplicate_descriptions: Counter[str] = Counter()

    while queue and len(visited) < max_pages:
        current = queue.popleft()
        if current in visited:
            continue
        visited.add(current)

        try:
            response = await _fetch_url(current)
            final_url = str(response.url)
            content_type = response.headers.get("content-type", "").lower()
            if "html" not in content_type and "<html" not in response.text[:500].lower():
                page_summaries.append({
                    "url": final_url,
                    "status": response.status_code,
                    "issues": [f"Non-HTML response ({content_type or 'unknown content type'})"],
                    "title": "",
                    "description": "",
                })
                continue

            analysis = _analyze_html_document(final_url, response.status_code, dict(response.headers), response.text)
            page_summaries.append({
                "url": final_url,
                "status": response.status_code,
                "issues": analysis["issues"],
                "notes": analysis["notes"],
                "title": analysis["title"],
                "description": analysis["meta_description"],
            })

            if analysis["title"]:
                duplicate_titles[analysis["title"]] += 1
            if analysis["meta_description"]:
                duplicate_descriptions[analysis["meta_description"]] += 1

            for discovered in _iter_internal_links(final_url, response.text):
                if discovered.startswith(origin) and discovered not in visited and discovered not in queue and len(visited) + len(queue) < max_pages * 3:
                    queue.append(discovered)
        except Exception as exc:
            page_summaries.append({
                "url": current,
                "status": "ERROR",
                "issues": [f"Fetch error: {str(exc)}"],
                "title": "",
                "description": "",
            })

    missing_titles = [page["url"] for page in page_summaries if "Missing <title>" in page.get("issues", [])]
    missing_descriptions = [page["url"] for page in page_summaries if "Missing meta description" in page.get("issues", [])]
    noindex_pages = [page["url"] for page in page_summaries if "Page is explicitly marked noindex" in page.get("issues", [])]
    duplicate_title_items = [(title, count) for title, count in duplicate_titles.items() if title and count > 1]
    duplicate_description_items = [(desc, count) for desc, count in duplicate_descriptions.items() if desc and count > 1]

    lines = [
        f"Crawl SEO audit for {normalized_start}",
        f"Origin: {origin}",
        f"Pages crawled: {len(page_summaries)} / {max_pages}",
        f"Pages missing title: {len(missing_titles)}",
        f"Pages missing meta description: {len(missing_descriptions)}",
        f"Pages marked noindex: {len(noindex_pages)}",
        f"Duplicate titles: {len(duplicate_title_items)}",
        f"Duplicate meta descriptions: {len(duplicate_description_items)}",
    ]

    if missing_titles:
        lines.append("Missing titles:")
        for item in missing_titles[:10]:
            lines.append(f"  {item}")
    if missing_descriptions:
        lines.append("Missing meta descriptions:")
        for item in missing_descriptions[:10]:
            lines.append(f"  {item}")
    if noindex_pages:
        lines.append("Noindex pages:")
        for item in noindex_pages[:10]:
            lines.append(f"  {item}")
    if duplicate_title_items:
        lines.append("Duplicate titles:")
        for title, count in duplicate_title_items[:10]:
            lines.append(f"  {count} pages -> {_clip(title, 120)}")
    if duplicate_description_items:
        lines.append("Duplicate meta descriptions:")
        for description, count in duplicate_description_items[:10]:
            lines.append(f"  {count} pages -> {_clip(description, 140)}")

    issue_pages = [page for page in page_summaries if page.get("issues")]
    if issue_pages:
        lines.append("Pages with issues:")
        for page in issue_pages[:10]:
            lines.append(f"  {page['url']} -> {', '.join(page['issues'])}")

    return "\n".join(lines)


@mcp.tool()
async def audit_live_site(url: str, crawl_pages: int = 5, include_lighthouse: bool = False) -> str:
    """
    Run a live SEO audit without requiring Search Console access.
    Combines page analysis, robots.txt inspection, sitemap discovery, PSI data, and a small same-origin crawl.

    Args:
        url: Full site/page URL
        crawl_pages: Number of pages to crawl for duplicate/missing-tag issues
        include_lighthouse: Whether to also run a local Lighthouse CLI audit
    """
    normalized_url = _ensure_https_url(url)
    origin = _origin_from_url(normalized_url)
    lines = [f"Live SEO audit for {normalized_url}", "=" * 80]

    lines.append(await analyze_page_seo(normalized_url))
    lines.append("\n" + "-" * 80)
    lines.append(await inspect_robots_txt(origin))
    lines.append("\n" + "-" * 80)

    robots_url = origin.rstrip("/") + "/robots.txt"
    discovered_sitemap = None
    try:
        robots_response = await _fetch_url(robots_url)
        for raw_line in robots_response.text.splitlines():
            line = raw_line.split("#", 1)[0].strip()
            if line.lower().startswith("sitemap:"):
                discovered_sitemap = line.split(":", 1)[1].strip()
                break
    except Exception:
        discovered_sitemap = None

    if discovered_sitemap:
        lines.append(await analyze_sitemap(discovered_sitemap, sample_urls=3))
    else:
        lines.append("No sitemap discovered via robots.txt.")
    lines.append("\n" + "-" * 80)

    lines.append(await get_pagespeed_insights(normalized_url))
    lines.append("\n" + "-" * 80)
    lines.append(await crawl_site_seo(normalized_url, max_pages=max(1, min(crawl_pages, 25))))

    if include_lighthouse:
        lines.append("\n" + "-" * 80)
        lines.append(await run_lighthouse_audit(normalized_url))

    lines.append("\n" + "=" * 80)
    return "\n".join(lines)


@mcp.tool()
async def find_striking_distance_keywords(site_url: str, days: int = 28, min_impressions: int = 10, row_limit: int = 50) -> str:
    """
    Find "striking distance" keywords — queries ranking at positions 5-20 with decent impressions.
    These are quick-win optimization targets that could reach page 1 with small improvements.

    Args:
        site_url: Exact GSC property URL
        days: Days to look back (default: 28)
        min_impressions: Minimum impressions to include (default: 10)
        row_limit: Max results (default: 50)
    """
    try:
        service = get_gsc_service()
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=days)

        response = service.searchanalytics().query(
            siteUrl=site_url,
            body={
                "startDate": start_date.strftime("%Y-%m-%d"),
                "endDate": end_date.strftime("%Y-%m-%d"),
                "dimensions": ["query", "page"],
                "rowLimit": 5000,
                "dataState": DATA_STATE,
            },
        ).execute()

        if not response.get("rows"):
            return f"No data found for {site_url}."

        # Filter for striking distance: position 5-20, decent impressions
        candidates = []
        for row in response["rows"]:
            pos = row.get("position", 0)
            imp = row.get("impressions", 0)
            if 5 <= pos <= 20 and imp >= min_impressions:
                candidates.append({
                    "query": row["keys"][0],
                    "page": row["keys"][1],
                    "clicks": row.get("clicks", 0),
                    "impressions": imp,
                    "ctr": row.get("ctr", 0),
                    "position": pos,
                    "potential": imp * 0.3 - row.get("clicks", 0),  # Estimated additional clicks if reaching top 3
                })

        candidates.sort(key=lambda x: x["potential"], reverse=True)

        lines = [
            f"Striking Distance Keywords for {site_url} (last {days} days):",
            f"Found {len(candidates)} keywords at positions 5-20 with {min_impressions}+ impressions",
            "-" * 100,
            "Query | Page | Pos | Impressions | Clicks | CTR | Est. Potential Clicks",
            "-" * 100,
        ]

        for item in candidates[:row_limit]:
            page_short = item["page"].split("//", 1)[-1] if "//" in item["page"] else item["page"]
            lines.append(
                f"{item['query'][:50]} | {page_short[:40]} | {item['position']:.1f} | "
                f"{item['impressions']} | {item['clicks']} | {item['ctr'] * 100:.1f}% | "
                f"+{max(0, item['potential']):.0f}"
            )

        if candidates:
            lines.append(f"\nTop opportunity: '{candidates[0]['query']}' at position {candidates[0]['position']:.1f}")
            lines.append(f"Currently getting {candidates[0]['clicks']} clicks from {candidates[0]['impressions']} impressions.")
            lines.append("Moving to top 3 could capture ~30% CTR.")

        return "\n".join(lines)
    except Exception as e:
        if "404" in str(e):
            return _site_not_found_error(site_url)
        return f"Error: {str(e)}"


@mcp.tool()
async def detect_cannibalization(site_url: str, days: int = 28, min_impressions: int = 5) -> str:
    """
    Detect keyword cannibalization — queries where multiple pages compete for the same keyword.
    This dilutes ranking power and confuses Google about which page to show.

    Args:
        site_url: Exact GSC property URL
        days: Days to look back (default: 28)
        min_impressions: Minimum impressions per query-page pair (default: 5)
    """
    try:
        service = get_gsc_service()
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=days)

        response = service.searchanalytics().query(
            siteUrl=site_url,
            body={
                "startDate": start_date.strftime("%Y-%m-%d"),
                "endDate": end_date.strftime("%Y-%m-%d"),
                "dimensions": ["query", "page"],
                "rowLimit": 10000,
                "dataState": DATA_STATE,
            },
        ).execute()

        if not response.get("rows"):
            return f"No data found for {site_url}."

        # Group by query
        query_pages = {}
        for row in response["rows"]:
            query = row["keys"][0]
            page = row["keys"][1]
            imp = row.get("impressions", 0)
            if imp >= min_impressions:
                if query not in query_pages:
                    query_pages[query] = []
                query_pages[query].append({
                    "page": page,
                    "clicks": row.get("clicks", 0),
                    "impressions": imp,
                    "position": row.get("position", 0),
                    "ctr": row.get("ctr", 0),
                })

        # Find queries with 2+ pages
        cannibalized = {q: pages for q, pages in query_pages.items() if len(pages) >= 2}

        # Sort by total impressions
        sorted_queries = sorted(
            cannibalized.items(),
            key=lambda x: sum(p["impressions"] for p in x[1]),
            reverse=True,
        )

        lines = [
            f"Keyword Cannibalization Report for {site_url} (last {days} days):",
            f"Found {len(cannibalized)} queries with multiple competing pages",
            "-" * 100,
        ]

        for query, pages in sorted_queries[:20]:
            total_imp = sum(p["impressions"] for p in pages)
            total_clicks = sum(p["clicks"] for p in pages)
            lines.append(f"\nQuery: '{query}' ({len(pages)} pages, {total_imp} total impressions, {total_clicks} clicks)")
            pages.sort(key=lambda x: x["impressions"], reverse=True)
            for p in pages:
                short_page = p["page"].split("//", 1)[-1] if "//" in p["page"] else p["page"]
                lines.append(
                    f"  {short_page[:60]} | pos: {p['position']:.1f} | imp: {p['impressions']} | "
                    f"clicks: {p['clicks']} | CTR: {p['ctr'] * 100:.1f}%"
                )

        if not cannibalized:
            lines.append("No keyword cannibalization detected. Each query maps to a single page.")

        return "\n".join(lines)
    except Exception as e:
        if "404" in str(e):
            return _site_not_found_error(site_url)
        return f"Error: {str(e)}"


@mcp.tool()
async def split_branded_queries(site_url: str, brand_name: str, days: int = 28) -> str:
    """
    Split search performance into branded vs non-branded queries.
    Shows true organic SEO growth by separating brand searches.

    Args:
        site_url: Exact GSC property URL
        brand_name: Your brand name to filter (e.g. "cdljobscenter")
        days: Days to look back (default: 28)
    """
    try:
        service = get_gsc_service()
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=days)
        date_range = {"startDate": start_date.strftime("%Y-%m-%d"), "endDate": end_date.strftime("%Y-%m-%d")}

        # Get branded queries
        branded = service.searchanalytics().query(
            siteUrl=site_url,
            body={
                **date_range, "dimensions": ["query"], "rowLimit": 500, "dataState": DATA_STATE,
                "dimensionFilterGroups": [{"filters": [
                    {"dimension": "query", "operator": "includingRegex", "expression": f"(?i){brand_name}"}
                ]}],
            },
        ).execute()

        # Get non-branded queries
        non_branded = service.searchanalytics().query(
            siteUrl=site_url,
            body={
                **date_range, "dimensions": ["query"], "rowLimit": 500, "dataState": DATA_STATE,
                "dimensionFilterGroups": [{"filters": [
                    {"dimension": "query", "operator": "excludingRegex", "expression": f"(?i){brand_name}"}
                ]}],
            },
        ).execute()

        # Also get totals
        total = service.searchanalytics().query(
            siteUrl=site_url,
            body={**date_range, "dimensions": [], "rowLimit": 1, "dataState": DATA_STATE},
        ).execute()

        def sum_metrics(rows):
            clicks = sum(r.get("clicks", 0) for r in rows)
            imp = sum(r.get("impressions", 0) for r in rows)
            ctr = (clicks / imp * 100) if imp > 0 else 0
            return clicks, imp, ctr

        branded_rows = branded.get("rows", [])
        non_branded_rows = non_branded.get("rows", [])
        total_rows = total.get("rows") or [{}]
        total_row = total_rows[0] if total_rows else {}

        b_clicks, b_imp, b_ctr = sum_metrics(branded_rows)
        nb_clicks, nb_imp, nb_ctr = sum_metrics(non_branded_rows)
        t_clicks = total_row.get("clicks", 0)
        t_imp = total_row.get("impressions", 0)

        lines = [
            f"Branded vs Non-Branded for {site_url} (last {days} days):",
            f"Brand filter: '{brand_name}'",
            "-" * 60,
            f"{'':20} | {'Clicks':>8} | {'Impressions':>12} | {'CTR':>6}",
            "-" * 60,
            f"{'Branded':20} | {b_clicks:>8,} | {b_imp:>12,} | {b_ctr:>5.1f}%",
            f"{'Non-Branded':20} | {nb_clicks:>8,} | {nb_imp:>12,} | {nb_ctr:>5.1f}%",
            f"{'Total':20} | {t_clicks:>8,} | {t_imp:>12,} | {(t_clicks / t_imp * 100) if t_imp else 0:>5.1f}%",
            "-" * 60,
            f"Non-branded share: {(nb_clicks / t_clicks * 100) if t_clicks else 0:.0f}% of clicks, {(nb_imp / t_imp * 100) if t_imp else 0:.0f}% of impressions",
        ]

        if non_branded_rows:
            lines.append(f"\nTop non-branded queries:")
            non_branded_rows.sort(key=lambda x: x.get("clicks", 0), reverse=True)
            for row in non_branded_rows[:10]:
                q = row["keys"][0][:50]
                lines.append(f"  {q} | clicks: {row.get('clicks', 0)} | imp: {row.get('impressions', 0)} | pos: {row.get('position', 0):.1f}")

        return "\n".join(lines)
    except Exception as e:
        if "404" in str(e):
            return _site_not_found_error(site_url)
        return f"Error: {str(e)}"


@mcp.tool()
async def site_audit(site_url: str, sitemap_url: str = None, max_inspect: int = 30) -> str:
    """
    Run a comprehensive site audit: checks sitemap health, inspects URLs for indexing issues,
    identifies coverage problems, and reports findings.

    Args:
        site_url: Exact GSC property URL (e.g. "sc-domain:example.com")
        sitemap_url: Optional sitemap URL. If not provided, auto-detects from GSC.
        max_inspect: Max URLs to inspect (default: 30, costs 1 API call each)
    """
    try:
        service = get_gsc_service()
        lines = [f"Site Audit Report for {site_url}", "=" * 80, f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n"]

        # 1. Sitemap health
        lines.append("1. SITEMAP HEALTH")
        lines.append("-" * 40)
        sitemaps = service.sitemaps().list(siteUrl=site_url).execute()
        sm_list = sitemaps.get("sitemap", [])

        if not sm_list:
            lines.append("WARNING: No sitemaps found!")
        else:
            for sm in sm_list:
                errors = int(sm.get("errors", 0))
                warnings = int(sm.get("warnings", 0))
                url_count = "N/A"
                for c in sm.get("contents", []):
                    if c.get("type") == "web":
                        url_count = c.get("submitted", "0")
                status = "OK" if errors == 0 else f"ERRORS: {errors}"
                lines.append(f"  {sm['path']} | {url_count} URLs | {status} | Warnings: {warnings}")

                if not sitemap_url:
                    sitemap_url = sm["path"]

        # 2. Performance summary
        lines.append(f"\n2. PERFORMANCE SUMMARY (last 28 days)")
        lines.append("-" * 40)
        end_date = datetime.now().date()
        start_date = end_date - timedelta(days=28)
        date_range = {"startDate": start_date.strftime("%Y-%m-%d"), "endDate": end_date.strftime("%Y-%m-%d")}

        total = service.searchanalytics().query(
            siteUrl=site_url,
            body={**date_range, "dimensions": [], "rowLimit": 1, "dataState": DATA_STATE},
        ).execute()

        if total.get("rows"):
            r = total["rows"][0]
            lines.append(f"  Clicks: {r.get('clicks', 0):,}")
            lines.append(f"  Impressions: {r.get('impressions', 0):,}")
            lines.append(f"  Avg CTR: {r.get('ctr', 0) * 100:.2f}%")
            lines.append(f"  Avg Position: {r.get('position', 0):.1f}")

        # 3. Top pages
        lines.append(f"\n3. TOP PAGES BY CLICKS")
        lines.append("-" * 40)
        pages = service.searchanalytics().query(
            siteUrl=site_url,
            body={**date_range, "dimensions": ["page"], "rowLimit": 20, "dataState": DATA_STATE},
        ).execute()

        top_urls_to_inspect = []
        for row in pages.get("rows", []):
            page = row["keys"][0]
            short = page.split("//", 1)[-1] if "//" in page else page
            lines.append(f"  {short[:60]} | clicks: {row.get('clicks', 0)} | imp: {row.get('impressions', 0)} | pos: {row.get('position', 0):.1f}")
            top_urls_to_inspect.append(page)

        # 4. URL inspection of top pages
        lines.append(f"\n4. INDEXING STATUS (inspecting up to {max_inspect} URLs)")
        lines.append("-" * 40)

        urls_to_inspect = top_urls_to_inspect[:max_inspect]
        categories = {"indexed": 0, "crawled_not_indexed": 0, "not_found": 0, "other": 0}
        issues = []

        for url in urls_to_inspect:
            try:
                result = service.urlInspection().index().inspect(
                    body={"inspectionUrl": url, "siteUrl": site_url}
                ).execute()
                idx = result.get("inspectionResult", {}).get("indexStatusResult", {})
                verdict = idx.get("verdict", "UNKNOWN")
                coverage = idx.get("coverageState", "")

                if verdict == "PASS":
                    categories["indexed"] += 1
                elif "not indexed" in coverage.lower():
                    categories["crawled_not_indexed"] += 1
                    issues.append(f"CRAWLED NOT INDEXED: {url}")
                elif "not found" in coverage.lower():
                    categories["not_found"] += 1
                    issues.append(f"NOT FOUND: {url}")
                else:
                    categories["other"] += 1
                    issues.append(f"{coverage}: {url}")

                # Check canonical mismatch
                gc = idx.get("googleCanonical", "")
                uc = idx.get("userCanonical", "")
                if gc and uc and gc != uc:
                    issues.append(f"CANONICAL MISMATCH: {url} (Google chose {gc}, you declared {uc})")

                await asyncio.sleep(0.15)
            except Exception as e:
                issues.append(f"INSPECT ERROR: {url} ({str(e)[:50]})")

        lines.append(f"  Indexed: {categories['indexed']}")
        lines.append(f"  Crawled not indexed: {categories['crawled_not_indexed']}")
        lines.append(f"  Not found: {categories['not_found']}")
        lines.append(f"  Other: {categories['other']}")

        if issues:
            lines.append(f"\n5. ISSUES FOUND ({len(issues)})")
            lines.append("-" * 40)
            for issue in issues:
                lines.append(f"  {issue}")
        else:
            lines.append("\n5. No issues found! All inspected pages are properly indexed.")

        lines.append("\n" + "=" * 80)
        lines.append("End of audit report.")

        return "\n".join(lines)
    except Exception as e:
        if "404" in str(e):
            return _site_not_found_error(site_url)
        return f"Error running audit: {str(e)}"


# ──────────────────────────────────────────────────────────────────────────────
# Auth Management
# ──────────────────────────────────────────────────────────────────────────────

@mcp.tool()
async def reauthenticate() -> str:
    """
    Perform a logout and new login sequence.
    Deletes the current OAuth token and triggers a new browser auth flow.
    """
    try:
        global _gsc_service_cache, _indexing_service_cache
        _gsc_service_cache = None
        _indexing_service_cache = None

        if os.path.exists(TOKEN_FILE):
            os.remove(TOKEN_FILE)

        if not os.path.exists(OAUTH_CLIENT_SECRETS_FILE):
            return "Error: client_secrets.json not found. Cannot start auth flow."

        flow = InstalledAppFlow.from_client_secrets_file(OAUTH_CLIENT_SECRETS_FILE, GSC_SCOPES)
        creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as token:
            token.write(creds.to_json())

        return "Successfully authenticated with a new Google account."
    except Exception as e:
        return f"Error during reauthentication: {str(e)}"


# ──────────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────────

def main():
    """Entry point for the MCP server (used by pyproject.toml [project.scripts])."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
