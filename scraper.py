import os
import time
import requests
from bs4 import BeautifulSoup
from supabase import create_client, Client
from datetime import datetime

# Supabase Configurations
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Supabase URL and Key must be provided!")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
}

def fetch_with_retry(url, max_retries=5, delay=10):
    """Link එක Load වෙනකම් 5 පාරක් Retry කරන Function එක"""
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(url, headers=headers, timeout=20)
            if response.status_code == 200:
                return response, attempt
        except Exception as e:
            print(f"[Attempt {attempt}/{max_retries}] Failed to fetch {url}: {e}")
            if attempt < max_retries:
                time.sleep(delay)
    return None, max_retries

def process_scraping():
    # 1. Target Websites ටික Supabase එකෙන් ලබා ගැනීම
    response = supabase.table('target_organizations').select('*').eq('is_active', True).execute()
    targets = response.data

    print(f"Found {len(targets)} active sites to scrape.")

    for target in targets:
        company_name = target['company_name']
        url = target['careers_url']
        print(f"\nScanning: {company_name} ({url})")

        res, retries_done = fetch_with_retry(url, max_retries=5, delay=12)

        if not res:
            # Retry 5කට පස්සෙත් Fail වුණොත් Table 3 (scraper_errors) එකට දානවා
            print(f"Error: {company_name} failed after 5 retries.")
            supabase.table('scraper_errors').insert({
                "company_name": company_name,
                "web_link": url,
                "error_message": "Failed to connect after 5 retries / Timeout",
                "retry_count": retries_done,
                "searched_at": datetime.now().isoformat()
            }).execute()
            continue

        # HTML Parsing
        soup = BeautifulSoup(res.content, 'html.parser')
        
        # NOTE: මෙතනට ආයතනයෙන් ආයතනයට අදාළ Scrape Logic එක/Keywords එකතු වේ.
        # උදාහරණයක් ලෙස 'vacancy', 'career', 'job', 'pdf' වැනි වචන සෙවීම:
        job_links = []
        for a in soup.find_all('a', href=True):
            text = a.text.strip().lower()
            href = a['href']
            if any(k in text or k in href.lower() for k in ['job', 'vacancy', 'opening', 'recruit', 'pdf', 'gazette']):
                full_link = href if href.startswith('http') else requests.compat.urljoin(url, href)
                job_links.append((a.text.strip() or "Vacancy Notice", full_link))

        if job_links:
            print(f"Found {len(job_links)} vacancies for {company_name}")
            for title, link in job_links:
                is_file = link.endswith('.pdf') or link.endswith('.jpg') or link.endswith('.png')
                
                # Google Drive Upload Logic (අපිට Drive API Connect කරපු ගමන් මෙතනට File Link එක ලබාදිය හැක)
                file_link = link # දැනට Direct Link එක දමනු ලැබේ

                # Table 1 (vacancies) එකට Insert කිරීම
                supabase.table('vacancies').insert({
                    "company_name": company_name,
                    "post_title": title,
                    "extract_date": datetime.now().strftime('%Y-%m-%d'),
                    "closing_date": "N/A", # Target Site එකෙන් ගන්න පුළුවන් නම්
                    "web_link": url,
                    "file_link": file_link,
                    "is_file": is_file,
                    "searched_at": datetime.now().isoformat()
                }).execute()
        else:
            # Vacancies මුකුත් නැත්නම් Table 2 (no_vacancies) එකට දානවා
            print(f"No vacancies found for {company_name}")
            supabase.table('no_vacancies').insert({
                "company_name": company_name,
                "web_link": url,
                "searched_at": datetime.now().isoformat()
            }).execute()

if __name__ == "__main__":
    process_scraping()