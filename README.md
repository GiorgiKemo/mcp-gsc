# mcp-seo-audit

<!-- mcp-name: io.github.giorgikemo/mcp-seo-audit -->

A Model Context Protocol (MCP) server for SEO auditing with Google Search Console, Indexing API, Chrome UX Report, PageSpeed Insights, local Lighthouse, robots.txt checks, sitemap analysis, on-page SEO inspection, crawl audits, and live site analysis. Works with Claude Code, Claude Desktop, Cursor, and any MCP-compatible client.

Forked from [AminForou/mcp-gsc](https://github.com/AminForou/mcp-gsc) and expanded into a broader technical SEO and performance audit server with 30 tools and a full test suite.

<a href="https://glama.ai/mcp/servers/GiorgiKemo/mcp-seo-audit">
  <img width="380" height="200" src="https://glama.ai/mcp/servers/GiorgiKemo/mcp-seo-audit/badge" alt="mcp-seo-audit MCP server" />
</a>

---

## What It Does

| Category | Tools | Description |
|----------|-------|-------------|
| **Property Management** | `list_properties`, `add_site`, `delete_site` | List, add, and remove GSC properties |
| **Search Analytics** | `get_search_analytics`, `get_advanced_search_analytics`, `get_performance_overview`, `get_search_by_page_query`, `compare_search_periods` | Query clicks, impressions, CTR, position with filtering, dimensions, and period comparison |
| **URL Inspection** | `inspect_url`, `batch_inspect_urls` | Check indexing status, crawl info, canonical, robots for one or many URLs |
| **Indexing API** | `request_indexing`, `request_removal`, `check_indexing_notification`, `batch_request_indexing` | Submit/remove URLs from Google's index via the Indexing API |
| **Sitemaps** | `get_sitemaps`, `submit_sitemap`, `delete_sitemap` | List, submit, and delete sitemaps |
| **Core Web Vitals** | `get_core_web_vitals` | LCP, FID, CLS, INP, TTFB via the Chrome UX Report (CrUX) API |
| **Performance Audits** | `get_pagespeed_insights`, `run_lighthouse_audit` | Run PageSpeed Insights and local Lighthouse audits with category scores, failing audit summaries, and automatic Lighthouse fallback when PSI errors or quota fails |
| **Technical SEO** | `inspect_robots_txt`, `analyze_sitemap`, `analyze_page_seo`, `crawl_site_seo`, `audit_live_site` | Inspect robots.txt, validate sitemaps, extract on-page SEO signals, rank findings by severity, crawl internal pages, and run a live SEO audit without GSC access |
| **SEO Analysis** | `find_striking_distance_keywords`, `detect_cannibalization`, `split_branded_queries` | Find keywords at positions 5-20, detect pages competing for the same query, split branded vs non-branded traffic |
| **Site Audit** | `site_audit` | All-in-one report: sitemap health, indexing status, canonical mismatches, performance summary |
| **Auth** | `reauthenticate` | Switch Google accounts by clearing cached OAuth tokens |

**30 tools total.**

---

## Setup

### 1. Google API Credentials

#### OAuth (recommended)

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Enable the **Search Console API** and **Web Search Indexing API**
3. Create an **OAuth 2.0 Client ID** (Desktop app)
4. Download `client_secrets.json`

#### Service Account

1. Create a service account in Google Cloud Console
2. Download the JSON key file
3. Add the service account email to your GSC properties

### 2. Install

#### Option A: PyPI

MCP clients can run the PyPI package directly:

```bash
uvx mcp-seo-audit
```

Or install it as a persistent command:

```bash
pipx install mcp-seo-audit
```

#### Option B: Source checkout

```bash
git clone https://github.com/GiorgiKemo/mcp-seo-audit.git
cd mcp-seo-audit
python -m venv .venv

# Activate:
# macOS/Linux: source .venv/bin/activate
# Windows:     .venv\Scripts\activate

pip install -r requirements.txt
```

### 3. Configure Your MCP Client

#### Claude Code (`~/.claude/settings.json`)

With the PyPI package:

```json
{
  "mcpServers": {
    "seo-audit": {
      "command": "uvx",
      "args": ["mcp-seo-audit"],
      "env": {
        "GSC_OAUTH_CLIENT_SECRETS_FILE": "/path/to/client_secrets.json",
        "PAGESPEED_API_KEY": "your-google-api-key",
        "CRUX_API_KEY": "your-google-api-key"
      }
    }
  }
}
```

With a source checkout:

```json
{
  "mcpServers": {
    "seo-audit": {
      "command": "/path/to/mcp-seo-audit/.venv/bin/python",
      "args": ["/path/to/mcp-seo-audit/gsc_server.py"],
      "env": {
        "GSC_OAUTH_CLIENT_SECRETS_FILE": "/path/to/client_secrets.json",
        "PAGESPEED_API_KEY": "your-google-api-key",
        "CRUX_API_KEY": "your-google-api-key"
      }
    }
  }
}
```

#### Claude Desktop (`claude_desktop_config.json`)

Same JSON structure — see [Claude Desktop MCP docs](https://docs.anthropic.com/en/docs/claude-code/mcp) for config file location.

### 4. Optional: Performance API Keys

For field and lab performance data, set `CRUX_API_KEY` and `PAGESPEED_API_KEY` in the env block:

```json
"env": {
  "GSC_OAUTH_CLIENT_SECRETS_FILE": "/path/to/client_secrets.json",
  "CRUX_API_KEY": "your-google-api-key",
  "PAGESPEED_API_KEY": "your-google-api-key"
}
```

You can also set `GOOGLE_API_KEY`; the server uses it as the PageSpeed Insights fallback key.

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GSC_OAUTH_CLIENT_SECRETS_FILE` | OAuth | `client_secrets.json` | Path to OAuth client secrets |
| `GSC_CREDENTIALS_PATH` | Service account | `service_account_credentials.json` | Path to service account key |
| `GSC_SKIP_OAUTH` | No | `false` | Set to `true` to skip OAuth and use service account only |
| `GSC_DATA_STATE` | No | `all` | `all` = fresh data matching GSC dashboard, `final` = confirmed data (2-3 day lag) |
| `CRUX_API_KEY` | No | none | Google API key for Core Web Vitals (CrUX) |
| `PAGESPEED_API_KEY` | No | none | Google API key for PageSpeed Insights / Lighthouse API calls |
| `GOOGLE_API_KEY` | No | none | Fallback source for `PAGESPEED_API_KEY` |
| `LIGHTHOUSE_CHROME_PATH` | No | auto-detect | Optional explicit path to Chrome/Chromium for local Lighthouse CLI |
| `SEO_AUDIT_ENABLE_WRITE_TOOLS` | No | `false` | Enables mutating tools such as adding/removing GSC properties, sitemap submission/deletion, and Indexing API publish calls |
| `SEO_AUDIT_ALLOW_PRIVATE_URLS` | No | `false` | Allows live audit fetches against localhost/private/reserved IPs; keep disabled except trusted local testing |
| `SEO_AUDIT_MAX_FETCH_BYTES` | No | `5242880` | Maximum bytes read from a fetched page, robots.txt, or sitemap response |
| `SEO_AUDIT_MAX_REDIRECTS` | No | `5` | Maximum redirects followed by live audit fetches |
| `SEO_AUDIT_MAX_SITEMAP_URLS` | No | `50000` | Maximum sitemap URLs parsed from one sitemap document |
| `SEO_AUDIT_MAX_CRAWL_PAGES` | No | `100` | Hard upper bound for `crawl_site_seo` and `audit_live_site` crawls |
| `LIGHTHOUSE_BINARY` | No | auto-detect | Explicit Lighthouse executable path. Preferred over `npx` for repeatable audits |
| `SEO_AUDIT_ALLOW_NPX_LIGHTHOUSE` | No | `false` | Allows `npx --yes lighthouse` fallback when no local Lighthouse binary is available |
| `LIGHTHOUSE_NO_SANDBOX` | No | `false` | Adds Chrome `--no-sandbox`; use only in constrained containers that require it |

---

## Example Prompts

```
"List my GSC properties"
"Show search analytics for cdljobscenter.com last 28 days"
"Find striking distance keywords for my site"
"Detect keyword cannibalization"
"Run a full site audit"
"Check Core Web Vitals for cdljobscenter.com"
"Run PageSpeed Insights for https://example.com"
"Run a local Lighthouse audit for https://example.com"
"Inspect robots.txt for https://example.com"
"Analyze https://example.com/sitemap.xml"
"Analyze on-page SEO for https://example.com/jobs"
"Crawl https://example.com and report duplicate titles"
"Run a live SEO audit for https://example.com"
"Inspect indexing status of these URLs: /jobs, /companies, /pricing"
"Request indexing for https://mysite.com/new-page"
"Compare search performance this month vs last month"
```

---

## Tests

96 tests covering all 30 tools with mocked Google/API/web-audit calls:

```bash
# Activate venv first
python -m pytest test_gsc_server.py -v
```

---

## What Changed From the Original

- **30 tools** — added PSI, local Lighthouse, robots.txt inspection, sitemap validation, page SEO analysis, crawl audits, and live site audits
- **Robustness fixes** — OAuth scope alignment, sort direction mapping, origin/URL detection, sitemap BOM and plain-text handling, external redirect crawl guards, empty rows handling, API key redaction, service caching, and stale cache handling on reauth
- **96-test QA suite** — coverage for GSC, CrUX, PSI, Lighthouse CLI, fallback behavior, robots, sitemaps, crawl audits, safety gates, auth scopes, and live-audit composition
- **Security** — private/local network fetches blocked by default, mutating Google tools gated behind an explicit env flag, and API keys redacted from error messages
- **Performance** — Google API service objects cached, async sleep instead of blocking, async CrUX HTTP calls, PSI-to-Lighthouse fallback, plus lab-performance tooling on top of CrUX field data
- **Audit quality** — local-preview aware canonical warnings, visible-content parsing that ignores `noscript` duplicates, severity-ranked SEO findings, image alt audits, crawlable-link checks, invalid JSON-LD detection, viewport/lang checks, and stricter canonical/robots diagnostics

---

## License

MIT. See [LICENSE](LICENSE).

Based on [AminForou/mcp-gsc](https://github.com/AminForou/mcp-gsc).
