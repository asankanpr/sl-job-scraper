import os
import re
import time
import json
import tempfile
import urllib3
import ssl
import requests
import threading
import io
from bs4 import BeautifulSoup
from supabase import create_client, Client
from datetime import datetime
from urllib.parse import urljoin, urlparse
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context
from concurrent.futures import ThreadPoolExecutor, as_completed
from rapidfuzz import fuzz
from pypdf import PdfReader

# Google GenAI SDK Setup (Dedicated Key Priority)
GEMINI_API_KEY = os.environ.get("GEMINI_SCRAPER_KEY", "").strip() or os.environ.get("GEMINI_API_KEY", "").strip()
ai_client = None
if GEMINI_API_KEY:
    try:
        from google import genai
        ai_client = genai.Client(api_key=GEMINI_API_KEY)
        print("✨ Scraper Gemini AI initialized successfully!")
    except Exception as e:
        print(f"⚠️ Gemini AI setup warning: {e}")

# Gemini Model Fallback Chain
DEFAULT_CHAIN = [
    "gemini-3.8-flash",       # Tier 1: Primary Model
    "gemini-3.6-flash",       # Tier 2: Backup
    "gemini-3.5-flash",       # Tier 3: Workhorse
    "gemini-3.5-flash-lite",  # Tier 4: Lite
    "gemini-3.1-flash-lite"   # Tier 5: High Buffer
]

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
gdrive_lock = threading.Lock()

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

def get_gdrive_service():
    if not GDRIVE_JSON or not GDRIVE_FOLDER_ID:
        return None
    try:
        info = json.loads(GDRIVE_JSON)
        creds = Credentials.from_service_account_info(
            info, scopes=['https://www.googleapis.com/auth/drive.file']
        )
        return build('drive', 'v3', credentials=creds)
    except Exception as e:
        print(f"❌ Google Drive Auth Error: {e}")
        return None

def upload_file_to_drive(service, file_path, file_name, is_image=False):
    if not service:
        return None
    with gdrive_lock:
        try:
            file_metadata = {'name': file_name, 'parents': [GDRIVE_FOLDER_ID]}
            media = MediaFileUpload(file_path, resumable=True)
            uploaded = service.files().create(body=file_metadata, media_body=media, fields='id, webViewLink').execute()
            file_id = uploaded.get('id')

            service.permissions().create(
                fileId=file_id, body={'type': 'anyone', 'role': 'reader'}
            ).execute()

            if is_image:
                return f"https://lh3.googleusercontent.com/d/{file_id}"
            else:
                return uploaded.get('webViewLink', f"https://drive.google.com/file/d/{file_id}/view")
        except Exception as e:
            print(f"⚠️ Drive Upload Error for {file_name}: {e}")
            return None

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
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
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

# Keywords & STRICT JUNK FILTERS
JOB_KEYWORDS = [
    'vacancy', 'vacancies', 'career', 'careers', 'opening', 'openings',
    'recruit', 'recruitment', 'employment', 'job', 'jobs', 'gazette',
    'post of', 'officer', 'executive', 'assistant', 'lecturer', 'manager',
    'ඇබෑර්තු', 'රැකියා', 'ගැසට්'
]

STRICT_JUNK_KEYWORDS = [
    'tender', 'tenders', 'quotation', 'bids', 'bid', 'procurement', 'supplier',
    'result', 'results', 'timetable', 'seminar', 'workshop',
    'auction', 'application form', 'specimen application', 'seniority list',
    'transfer', 'minutes', 'amendment', 'circular', 'syllabus', 'viva',
    'press release', 'news', 'notice board', 'procurement notice',
    'ප්‍රසම්පාදන', 'ලංසු', 'ප්‍රතිඵල', 'අයදුම්පත', 'ආකෘතිය', 'ජ්‍යෙෂ්ඨතාව',
    'වෙන්දේසිය', 'මාරුවීම්', 'වාර්තාව'
]

HUB_KEYWORDS = ['career', 'careers', 'vacancy', 'vacancies', 'job', 'jobs', 'notice', 'notices', 'ඇබෑර්තු']

def extract_text_from_pdf_bytes(pdf_bytes):
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = ""
        for page in reader.pages[:3]:
            text += (page.extract_text() or "") + "\n"
        return text.strip()
    except Exception:
        return ""

def analyze_content_with_ai(title_raw, text_content):
    if ai_client and len(text_content) > 20:
        prompt = f"""
        Analyze the following Sri Lankan job posting text and return JSON only:
        Title: {title_raw}
        Content: {text_content[:2000]}

        Output format:
        {{
            "is_valid_job_vacancy": true/false,
            "clean_post_title": "Clean concise job title",
            "closing_date": "YYYY-MM-DD or N/A",
            "salary": "Extracted salary/scale or N/A"
        }}
        Strict Rule:
        - is_valid_job_vacancy MUST BE FALSE if this is a procurement/tender notice, general public circular, exam result sheet, or blank specimen application form.
        """

        for model_name in DEFAULT_CHAIN:
            try:
                response = ai_client.models.generate_content(
                    model=model_name,
                    contents=prompt
                )
                raw = response.text.strip()
                raw_clean = re.sub(r'```json\s*|\s*```', '', raw)
                data = json.loads(raw_clean)
                return data
            except Exception:
                continue

    closing_date = "N/A"
    date_match = re.search(r'(\d{4}[-/.]\d{1,2}[-/.]\d{1,2})|(\d{1,2}[-/.]\d{1,2}[-/.]\d{4})', text_content)
    if date_match:
        raw_d = date_match.group(0).replace('.', '-').replace('/', '-')
        try:
            parts = raw_d.split('-')
            if len(parts[0]) == 4:
                parsed_d = datetime.strptime(raw_d, '%Y-%m-%d').date()
            else:
                parsed_d = datetime.strptime(raw_d, '%d-%m-%Y').date()
            closing_date = parsed_d.strftime('%Y-%m-%d')
        except Exception:
            pass

    salary = "N/A"
    sal_match = re.search(r'(?:Rs\.?|LKR)\s*[\d,]+(?:\s*-\s*[\d,]+)?|(?:Salary Scale|මාසික වේතනය)[\s:-]*[A-Z0-9/-]+', text_content, re.IGNORECASE)
    if sal_match:
        salary = sal_match.group(0).strip()

    clean_title = re.sub(r'\b(download|click here|pdf|view|application)\b', '', title_raw, flags=re.IGNORECASE).strip()
    if not clean_title or len(clean_title) < 3:
        clean_title = "Vacancy Notice"

    return {
        "is_valid_job_vacancy": True,
        "clean_post_title": clean_title,
        "closing_date": closing_date,
        "salary": salary
    }

def is_duplicate_vacancy(company_name, title, closing_date, salary, existing_records):
    norm_title = re.sub(r'[^a-zA-Z0-9]', '', title.lower())

    for rec in existing_records:
        rec_title = re.sub(r'[^a-zA-Z0-9]', '', (rec.get('post_title') or '').lower())
        sim_ratio = fuzz.token_sort_ratio(norm_title, rec_title)

        if sim_ratio > 85:
            rec_date = rec.get('closing_date') or 'N/A'
            rec_sal = rec.get('salary') or 'N/A'

            if closing_date != "N/A" and rec_date != "N/A":
                if closing_date == rec_date:
                    return True
                else:
                    continue
            elif salary != "N/A" and rec_sal != "N/A":
                if salary == rec_sal:
                    return True
            else:
                if sim_ratio > 95:
                    return True

    return False

def process_single_target(target, gdrive_service):
    session = get_thread_session()
    company_name = target['company_name']
    main_url = target['careers_url']
    print(f"⚡ [Scanning]: {company_name}")

    res, working_url = fetch_url_smart(session, main_url)
    if not res:
        supabase.table('scraper_errors').insert({
            "company_name": company_name,
            "web_link": main_url,
            "error_message": "Connection failure after smart retry",
            "searched_at": datetime.now().isoformat()
        }).execute()
        return

    soup = BeautifulSoup(res.content, 'html.parser')

    # Reads all records (both processed and unprocessed) to guarantee zero duplicates
    existing_resp = supabase.table('vacancies').select('post_title, closing_date, salary, web_link').eq('company_name', company_name).execute()
    existing_records = existing_resp.data or []

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

        embed_links = []
        for tag in p_soup.find_all(['iframe', 'embed', 'object']):
            src = tag.get('src') or tag.get('data')
            if src:
                embed_links.append((src, "Embedded Flyer Document"))

        a_links = [(a['href'], a.get_text(strip=True)) for a in p_soup.find_all('a', href=True)]
        all_candidate_links = embed_links + a_links

        for href, title_text in all_candidate_links:
            href_clean = href.strip()
            combined = f"{title_text} {href_clean}".lower()

            # Strict Junk Filtering
            if any(jk in combined for jk in STRICT_JUNK_KEYWORDS):
                continue

            if any(jk in combined for jk in JOB_KEYWORDS) or href_clean.lower().endswith(('.pdf', '.jpg', '.png', '.jpeg')):
                full_link = urljoin(p_url, href_clean)

                if full_link in seen_links or full_link.endswith('#') or 'javascript:' in full_link:
                    continue
                seen_links.add(full_link)

                is_file = full_link.lower().endswith(('.pdf', '.jpg', '.jpeg', '.png', '.webp'))
                is_image = full_link.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))

                text_content = combined
                if full_link.lower().endswith('.pdf'):
                    try:
                        pdf_res = session.get(full_link, timeout=10, verify=False)
                        if pdf_res.status_code == 200:
                            extracted_pdf_text = extract_text_from_pdf_bytes(pdf_res.content)
                            if len(extracted_pdf_text) > 30:
                                text_content += " " + extracted_pdf_text
                    except Exception:
                        pass

                parsed_info = analyze_content_with_ai(title_text or "Vacancy Notice", text_content)

                if not parsed_info.get("is_valid_job_vacancy", True):
                    continue

                clean_title = parsed_info.get("clean_post_title", "Job Vacancy")
                closing_date = parsed_info.get("closing_date", "N/A")
                salary = parsed_info.get("salary", "N/A")

                if is_duplicate_vacancy(company_name, clean_title, closing_date, salary, existing_records):
                    print(f"⏩ [Duplicate Skipped]: {clean_title} ({company_name})")
                    continue

                found_items.append({
                    "title": clean_title[:200],
                    "link": full_link,
                    "closing_date": closing_date,
                    "salary": salary,
                    "is_file": is_file,
                    "is_image": is_image
                })

    if found_items:
        print(f"✅ [Success]: {len(found_items)} verified vacancies for {company_name}")
        for item in found_items:
            drive_link = None
            if item['is_file'] and gdrive_service:
                try:
                    f_res = session.get(item['link'], timeout=15, verify=False)
                    if f_res.status_code == 200:
                        ext = ".jpg" if item['is_image'] else ".pdf"
                        temp_name = f"{company_name}_{int(time.time())}{ext}".replace(" ", "_")
                        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as temp_file:
                            temp_file.write(f_res.content)
                            temp_path = temp_file.name

                        drive_link = upload_file_to_drive(gdrive_service, temp_path, temp_name, is_image=item['is_image'])
                        os.remove(temp_path)
                except Exception:
                    pass

            final_link = drive_link or item['link']

            supabase.table('vacancies').insert({
                "company_name": company_name,
                "post_title": item['title'],
                "extract_date": datetime.now().strftime('%Y-%m-%d'),
                "closing_date": item['closing_date'],
                "salary": item['salary'],
                "web_link": item['link'],
                "file_link": final_link,
                "is_file": item['is_file'],
                "is_processed": False,
                "searched_at": datetime.now().isoformat()
            }).execute()

        supabase.table('scraper_errors').delete().eq('company_name', company_name).execute()
    else:
        print(f"ℹ️ [No Vacancies]: {company_name}")
        supabase.table('no_vacancies').insert({
            "company_name": company_name,
            "web_link": working_url,
            "searched_at": datetime.now().isoformat()
        }).execute()

def process_scraping():
    gdrive_service = get_gdrive_service()
    response = supabase.table('target_organizations').select('*').eq('is_active', True).execute()
    targets = response.data

    print(f"🚀 === Scraper Started for {len(targets)} Targets ===")
    start_time = time.time()

    max_workers = min(5, len(targets)) if targets else 1
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(process_single_target, target, gdrive_service) for target in targets]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as e:
                print(f"❌ Thread Error: {e}")

    print(f"🎉 === Finished in {round(time.time() - start_time, 2)} seconds ===")

if __name__ == "__main__":
    process_scraping()
