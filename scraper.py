import os
import re
import time
import json
import tempfile
import urllib3
import ssl
import requests
import threading
from bs4 import BeautifulSoup
from supabase import create_client, Client
from datetime import datetime
from urllib.parse import urljoin, urlparse
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context
from concurrent.futures import ThreadPoolExecutor, as_completed

# Google Drive Libraries
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

# Disable SSL Warnings
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Supabase Configurations
SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip()
GDRIVE_JSON = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON", "").strip()
GDRIVE_FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID", "").strip()

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Supabase URL and Key must be provided!")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Thread Lock for Google Drive API to prevent memory corruption (Exit Code 134 fix)
gdrive_lock = threading.Lock()

# --- 1. Google Drive Connection Setup ---
def get_gdrive_service():
    if not GDRIVE_JSON or not GDRIVE_FOLDER_ID:
        print("⚠️ Google Drive secrets not configured. Skipping Drive upload.")
        return None
    try:
        info = json.loads(GDRIVE_JSON)
        creds = Credentials.from_service_account_info(
            info, 
            scopes=['https://www.googleapis.com/auth/drive.file']
        )
        return build('drive', 'v3', credentials=creds)
    except Exception as e:
        print(f"❌ Google Drive Auth Error: {e}")
        return None

def upload_file_to_drive(service, file_path, file_name, is_image=False):
    if not service:
        return None
    with gdrive_lock:  # Safe Thread Locking for Google Drive API
        try:
            file_metadata = {
                'name': file_name,
                'parents': [GDRIVE_FOLDER_ID]
            }
            media = MediaFileUpload(file_path, resumable=True)
            uploaded = service.files().create(body=file_metadata, media_body=media, fields='id, webViewLink').execute()
            file_id = uploaded.get('id')

            # Public Permission
            service.permissions().create(
                fileId=file_id,
                body={'type': 'anyone', 'role': 'reader'}
            ).execute()

            # Direct Image Link (Option 2)
            if is_image:
                return f"https://lh3.googleusercontent.com/d/{file_id}"
            else:
                return uploaded.get('webViewLink', f"https://drive.google.com/file/d/{file_id}/view")
        except Exception as e:
            print(f"❌ Drive Upload Error for {file_name}: {e}")
            return None


# --- 2. Custom Legacy SSL & Thread-Safe Session ---
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

def get_thread_session():
    session = requests.Session()
    adapter = CustomSSLAdapter()
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9,si;q=0.8'
    })
    return session

def fetch_url_smart(session, original_url, max_retries=2):
    parsed = urlparse(original_url)
    netloc = parsed.netloc
    path = parsed.path or '/'
    query = f"?{parsed.query}" if parsed.query else ""

    domain_variants = [netloc]
    if netloc.startswith('www.'):
        domain_variants.append(netloc[4:])
    else:
        domain_variants.append(f"www.{netloc}")

    url_variants = []
    for d in domain_variants:
        url_variants.append(f"https://{d}{path}{query}")
        url_variants.append(f"http://{d}{path}{query}")

    for attempt in range(1, max_retries + 1):
        for target_url in url_variants:
            try:
                res = session.get(target_url, timeout=15, verify=False)
                if res.status_code == 200 and len(res.content) > 300:
                    return res, target_url
            except Exception:
                continue
        time.sleep(1)

    return None, original_url


# --- 3. Keyword Lists ---
JOB_KEYWORDS = [
    'vacancy', 'vacancies', 'career', 'careers', 'opening', 'openings',
    'recruit', 'recruitment', 'employment', 'job', 'jobs', 'gazette',
    'post of', 'officer', 'executive', 'assistant', 'lecturer', 'manager',
    'ඇබෑර්තු', 'රැකියා', 'ගැසට්', 'අයදුම්පත්', 'තැන්'
]

JUNK_KEYWORDS = [
    'tender', 'tenders', 'quotation', 'bids', 'bid', 'procurement', 'supplier',
    'exam', 'examination', 'result', 'results', 'timetable', 'seminar', 'workshop',
    'event', 'auction', 'ප්‍රසම්පාදන', 'ලංසු', 'විභාග', 'ප්‍රතිඵල', 'ලේඛන'
]

HUB_KEYWORDS = ['career', 'careers', 'vacancy', 'vacancies', 'job', 'jobs', 'notice', 'notices', 'ඇබෑර්තු']


# --- 4. Expiry Date Check ---
def extract_and_check_expiry(text_content):
    date_patterns = [
        r'(?:closing date|valid until|before|අවසාන දිනය|අවසන් දිනය)[\s:-]*(\d{4}[-/.]\d{1,2}[-/.]\d{1,2})',
        r'(?:closing date|valid until|before|අවසාන දිනය|අවසන් දිනය)[\s:-]*(\d{1,2}[-/.]\d{1,2}[-/.]\d{4})',
        r'(\d{4}[-/.]\d{1,2}[-/.]\d{1,2})',
        r'(\d{1,2}[-/.]\d{1,2}[-/.]\d{4})'
    ]

    for pattern in date_patterns:
        match = re.search(pattern, text_content, re.IGNORECASE)
        if match:
            raw_date = match.group(1).replace('.', '-').replace('/', '-')
            try:
                parts = raw_date.split('-')
                if len(parts[0]) == 4:
                    parsed_date = datetime.strptime(raw_date, '%Y-%m-%d').date()
                else:
                    parsed_date = datetime.strptime(raw_date, '%d-%m-%Y').date()
                
                is_expired = parsed_date < datetime.now().date()
                return parsed_date.strftime('%Y-%m-%d'), is_expired
            except Exception:
                continue

    return "N/A", False


# --- 5. Thread Worker Function (Per Site) ---
def process_single_target(target, gdrive_service):
    session = get_thread_session()
    company_name = target['company_name']
    main_url = target['careers_url']
    print(f"⚡ [Scanning Thread]: {company_name}")

    res, working_url = fetch_url_smart(session, main_url)

    if not res:
        print(f"❌ [Failed Connection]: {company_name}")
        supabase.table('scraper_errors').insert({
            "company_name": company_name,
            "web_link": main_url,
            "error_message": "Connection failure after smart retry",
            "retry_count": 5,
            "searched_at": datetime.now().isoformat()
        }).execute()
        return

    soup = BeautifulSoup(res.content, 'html.parser')

    # Hub Finder
    target_pages = [working_url]
    for a in soup.find_all('a', href=True):
        link_text = a.text.strip().lower()
        href = a['href'].lower()
        if any(hk in link_text or hk in href for hk in HUB_KEYWORDS):
            full_hub = urljoin(working_url, a['href'])
            if full_hub not in target_pages:
                target_pages.append(full_hub)

    found_items = []
    seen_links = set()

    for page_url in target_pages[:3]:
        p_res, p_url = fetch_url_smart(session, page_url)
        if not p_res:
            continue
        
        p_soup = BeautifulSoup(p_res.content, 'html.parser')

        for a in p_soup.find_all('a', href=True):
            href = a['href'].strip()
            title_text = a.get_text(strip=True)
            combined = f"{title_text} {href}".lower()

            # Junk Filter
            if any(jk in combined for jk in JUNK_KEYWORDS):
                continue

            # Vacancy Keyword Match
            if any(jk in combined for jk in JOB_KEYWORDS):
                full_link = urljoin(p_url, href)

                if full_link in seen_links or full_link.endswith('#') or 'javascript:' in full_link:
                    continue
                seen_links.add(full_link)

                display_title = title_text if len(title_text) > 4 else "Vacancy Notice"

                closing_date, is_expired = extract_and_check_expiry(combined)
                if is_expired:
                    continue

                is_file = full_link.lower().endswith(('.pdf', '.jpg', '.jpeg', '.png', '.webp'))
                is_image = full_link.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))

                found_items.append({
                    "title": display_title[:200],
                    "link": full_link,
                    "closing_date": closing_date,
                    "is_file": is_file,
                    "is_image": is_image
                })

    if found_items:
        print(f"✅ [Success]: Found {len(found_items)} items for {company_name}")
        for item in found_items:
            existing = supabase.table('vacancies').select('id').eq('company_name', company_name).eq('web_link', item['link']).execute()
            
            if not existing.data:
                drive_link = None

                # Google Drive Upload (Protected with Lock)
                if item['is_file'] and gdrive_service:
                    try:
                        f_res = session.get(item['link'], timeout=20, verify=False)
                        if f_res.status_code == 200:
                            ext = ".jpg" if item['is_image'] else ".pdf"
                            temp_name = f"{company_name}_{int(time.time())}{ext}".replace(" ", "_")
                            
                            with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as temp_file:
                                temp_file.write(f_res.content)
                                temp_path = temp_file.name

                            drive_link = upload_file_to_drive(gdrive_service, temp_path, temp_name, is_image=item['is_image'])
                            os.remove(temp_path)
                    except Exception as e:
                        print(f"⚠️ Could not upload to Drive ({company_name}): {e}")

                final_file_link = drive_link or item['link']

                supabase.table('vacancies').insert({
                    "company_name": company_name,
                    "post_title": item['title'],
                    "extract_date": datetime.now().strftime('%Y-%m-%d'),
                    "closing_date": item['closing_date'],
                    "web_link": item['link'],
                    "file_link": final_file_link,
                    "is_file": item['is_file'],
                    "searched_at": datetime.now().isoformat()
                }).execute()

        supabase.table('scraper_errors').delete().eq('company_name', company_name).execute()

    else:
        print(f"ℹ️ [No Vacancies]: No active vacancies found for {company_name}")
        supabase.table('no_vacancies').insert({
            "company_name": company_name,
            "web_link": working_url,
            "searched_at": datetime.now().isoformat()
        }).execute()


# --- 6. Main Parallel Controller ---
def process_scraping():
    gdrive_service = get_gdrive_service()

    response = supabase.table('target_organizations').select('*').eq('is_active', True).execute()
    targets = response.data

    print(f"🚀 === Starting Multi-threaded Fast Scraper for {len(targets)} active targets ===")
    start_time = time.time()

    # Parallel Execution (Workers = 5 to be safe with memory)
    max_workers = min(5, len(targets)) if targets else 1
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(process_single_target, target, gdrive_service) for target in targets]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                print(f"❌ Thread Error: {e}")

    elapsed_time = round(time.time() - start_time, 2)
    print(f"\n🎉 === Finished Scrape in {elapsed_time} seconds! ===")

if __name__ == "__main__":
    process_scraping()
