import os
import time
import requests
import urllib3
import ssl
from bs4 import BeautifulSoup
from supabase import create_client, Client
from datetime import datetime
from urllib.parse import urljoin, urlparse
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

# SSL Warnings Disable කිරීම
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Supabase Credentials
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Supabase URL and Key must be provided!")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# 1. Custom Legacy SSL Adapter (ලංකාවේ පැරණි Gov/Edu Servers සඳහා)
class CustomSSLAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            ctx.set_ciphers('DEFAULT@SECLEVEL=1')
        except Exception:
            pass
        kwargs['ssl_context'] = ctx
        return super().init_poolmanager(*args, **kwargs)

def get_smart_session():
    session = requests.Session()
    adapter = CustomSSLAdapter()
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9,si;q=0.8',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1'
    })
    return session

# 2. Smart Fallback Fetcher (Domain, HTTP/HTTPS, www වෙනස්කම් Auto Try කිරීම)
def fetch_url_smart(session, original_url, max_retries=3):
    parsed = urlparse(original_url)
    netloc = parsed.netloc
    path = parsed.path or '/'
    query = f"?{parsed.query}" if parsed.query else ""

    # Alternate Domain/Protocol Combinations සෑදීම
    domain_variants = [netloc]
    if netloc.startswith('www.'):
        domain_variants.append(netloc[4:])
    else:
        domain_variants.append(f"www.{netloc}")

    url_variants = []
    for d in domain_variants:
        url_variants.append(f"https://{d}{path}{query}")
        url_variants.append(f"http://{d}{path}{query}")

    # Unique list එකක් තබාගැනීම
    unique_urls = []
    for u in url_variants:
        if u not in unique_urls:
            unique_urls.append(u)

    last_error = ""
    for attempt in range(1, max_retries + 1):
        for target_url in unique_urls:
            try:
                res = session.get(target_url, timeout=25, verify=False)
                if res.status_code == 200 and len(res.content) > 300:
                    return res, target_url, attempt
            except Exception as e:
                last_error = str(e)
                continue
        time.sleep(3)

    return None, original_url, last_error

# 3. Tactical Deep Extractor (Sinhala & English Keywords)
KEYWORDS = [
    'vacancy', 'vacancies', 'career', 'careers', 'opening', 'openings',
    'recruit', 'recruitment', 'employment', 'job', 'jobs', 'gazette',
    'notice', 'notices', 'download', 'application', 'ඇබෑර්තු', 'රැකියා', 'ගැසට්'
]

def extract_vacancies(soup, base_url):
    extracted = []
    seen_links = set()

    for a in soup.find_all('a', href=True):
        href = a['href'].strip()
        text = a.get_text(strip=True)
        title_attr = a.get('title', '')
        
        combined_text = f"{text} {title_attr} {href}".lower()
        
        # Keyword match වීම පරීක්ෂා කිරීම
        if any(k in combined_text for k in KEYWORDS):
            full_url = urljoin(base_url, href)
            
            # Junk / Anchor links ඉවත් කිරීම
            if full_url in seen_links or full_url.endswith('#') or 'javascript:' in full_url:
                continue
            
            seen_links.add(full_url)
            
            # Post Title පිරිසිදු කරගැනීම
            display_title = text if len(text) > 3 else (title_attr or "Vacancy Notice / Advertisement")
            extracted.append({
                "title": display_title[:200],
                "link": full_url,
                "is_file": full_url.lower().endswith(('.pdf', '.jpg', '.jpeg', '.png', '.doc', '.docx'))
            })

    return extracted

# 4. Main Processing Workflow
def process_scraping():
    session = get_smart_session()

    # Target Sites ලබාගැනීම
    response = supabase.table('target_organizations').select('*').eq('is_active', True).execute()
    targets = response.data

    print(f"=== Starting Smart Scraper for {len(targets)} active targets ===")

    for target in targets:
        company_name = target['company_name']
        url = target['careers_url']
        print(f"\n[Scanning]: {company_name} ({url})")

        res, working_url, err_detail = fetch_url_smart(session, url, max_retries=3)

        if not res:
            print(f"❌ [Failed]: {company_name} after retries. Reason: {err_detail[:100]}")
            # Table 3 (scraper_errors) එකට Insert කිරීම
            supabase.table('scraper_errors').insert({
                "company_name": company_name,
                "web_link": url,
                "error_message": f"Connection Failure / Timeout: {err_detail[:150]}",
                "retry_count": 5,
                "searched_at": datetime.now().isoformat()
            }).execute()
            continue

        # HTML Parse කිරීම
        soup = BeautifulSoup(res.content, 'html.parser')
        vacancies = extract_vacancies(soup, working_url)

        if vacancies:
            print(f"✅ [Success]: Found {len(vacancies)} vacancy items for {company_name}")
            for item in vacancies:
                # Deduplication: කලින් මේ Record එක තියෙනවාදැයි බැලීම
                existing = supabase.table('vacancies').select('id').eq('company_name', company_name).eq('web_link', item['link']).execute()
                
                if not existing.data:
                    supabase.table('vacancies').insert({
                        "company_name": company_name,
                        "post_title": item['title'],
                        "extract_date": datetime.now().strftime('%Y-%m-%d'),
                        "closing_date": "N/A",
                        "web_link": working_url,
                        "file_link": item['link'],
                        "is_file": item['is_file'],
                        "searched_at": datetime.now().isoformat()
                    }).execute()
            
            # සාර්ථක වුණ නිසා පැරණි Errors තිබුණා නම් Clear කිරීම
            supabase.table('scraper_errors').delete().eq('company_name', company_name).execute()

        else:
            print(f"ℹ️ [No Vacancies]: No active vacancies found for {company_name}")
            supabase.table('no_vacancies').insert({
                "company_name": company_name,
                "web_link": working_url,
                "searched_at": datetime.now().isoformat()
            }).execute()

if __name__ == "__main__":
    process_scraping()
