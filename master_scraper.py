import os
import sys
import firebase_admin
from firebase_admin import credentials, firestore
import json
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from datetime import datetime
import re
import time
import hashlib

# ==========================================
# 1. CONFIGURATION & SETUP
# ==========================================

# Read from environment variable (GitHub Actions) or fallback to direct value (local)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "AIzaSyDpGjYlFVFjWFpspcfHeaH5H0-Qm8UgGGc")

# Write Firebase key from environment variable to a file (GitHub Actions mode)
_sa_json = os.environ.get("FIREBASE_SERVICE_ACCOUNT", "")
if _sa_json:
    with open("serviceAccountKey.json", "w") as _f:
        _f.write(_sa_json)

# --- FREE TIER OPTIMIZATIONS ---
REQUEST_DELAY = 7
RATE_LIMIT_COOLDOWN = 65

URL_MAP = {
    "VACANCY": "https://www.sarkariresult.com/latestjob.php",
    "ADMIT_CARD": "https://www.sarkariresult.com/admitcard.php",
    "RESULT": "https://www.sarkariresult.com/result.php",
    "ANSWER_KEY": "https://www.sarkariresult.com/answerkey.php"
}

# Initialize Firebase exactly once
cred = credentials.Certificate("serviceAccountKey.json")
if not firebase_admin._apps:
    firebase_admin.initialize_app(cred)
db = firestore.client()


# ==========================================
# 2. SHARED HELPER FUNCTIONS
# ==========================================
def get_schema_prompt(category):
    return f"""
    You are an expert data extraction assistant for an Indian government job portal.
    Read the following text scraped from a Sarkari Result page and extract the details into strict JSON format.

    Crucially, you must extract ALL factual data, including detailed post-wise tables, fees, and dates.
    If a detail is missing, use `null`. Do not invent information.

    Output strictly this JSON structure and nothing else. Do not use markdown blocks (```json).

    {{
      "id": "String (Leave exactly as 'auto-generated')",
      "title": "String (e.g., SSC CGL Tier 1 Admit Card 2026)",
      "vacancies": "String (e.g., 233 Post or 'Not Specified')",
      "dateText": "String (Format: 'Exam: DD-MMM-YYYY' or 'Closes: DD-MMM-YYYY')",
      "category": "{category}", 
      "sector": "String (Must be exactly one of: BANK, SSC, RAILWAY, UPSC, DEFENCE, ALL)",
      "importantDates": {{ 
          "Extract ALL date labels found (e.g., Application Begin, Last Date, Correction, Result, DV)": "String (e.g., 07/01/2026)"
      }},
      "applicationFee": {{ 
          "Extract ALL fee labels found (e.g., General/OBC, SC/ST, PH, Mains Fee)": "String (e.g., 200/-)"
      }},
      "baseAgeLimit": "String (Generic age summary, e.g., '18-27 Years as on 01/07/2026')",
      "subPosts": [
        {{
          "postName": "String",
          "department": "String",
          "totalVacancy": "String",
          "ageLimit": "String",
          "specificEligibility": "String"
        }}
      ]
    }}
    """


def extract_exact_links(job_soup):
    """Dynamically extracts exact link names and URLs, aggressively blocking promos and social media."""
    links = {}

    for tr in job_soup.find_all('tr'):
        a_tags = tr.find_all('a', href=True)
        tds = tr.find_all('td')

        if a_tags and len(tds) >= 2:
            raw_label = tds[0].get_text(separator=' ', strip=True)
            label = raw_label.replace("Click Here", "").replace("Link Activate", "").replace("Soon", "").strip()

            lower_label = label.lower()
            right_side_text = tds[1].get_text(separator=' ', strip=True).lower()

            junk_keywords = [
                "telegram", "whatsapp", "twitter", "x app", "facebook", "instagram", "youtube",
                "join our", "follow us", "channel",
                "apple", "ios", "android", "play store", "download app", "mobile app", "sarkari result app",
                "video", "how to", "notice", "extended", "resume", "photo", "typing test", "image resizer",
                "tools", "pdf portal", "remove background", "sarkari result portal"
            ]

            if not any(junk in lower_label for junk in junk_keywords) and not any(
                    junk in right_side_text for junk in junk_keywords) and label:

                if len(a_tags) > 1:
                    for a in a_tags:
                        sub_text = a.get_text(strip=True)
                        final_name = f"{label} ({sub_text})" if sub_text else label
                        if len(final_name) < 60:
                            links[final_name] = a['href']
                else:
                    if len(label) < 60:
                        links[label] = a_tags[0]['href']

    return links


def call_gemini_api(prompt, text, url):
    """Handles the API call with strict Free Tier pacing."""
    full_prompt = f"{prompt}\n\nURL: {url}\n\nTEXT:\n{text}"
    gemini_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-flash-lite-preview:generateContent?key={GEMINI_API_KEY}"
    payload = {"contents": [{"parts": [{"text": full_prompt}]}]}

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = requests.post(gemini_url, headers={'Content-Type': 'application/json', 'Connection': 'close'}, json=payload, timeout=60)

            if response.status_code == 429:
                print(f"   ⚠️ Free Tier Limit hit! Cooling down for {RATE_LIMIT_COOLDOWN}s...")
                time.sleep(RATE_LIMIT_COOLDOWN)
                continue

            response.raise_for_status()
            data = response.json()
            raw_text = data['candidates'][0]['content']['parts'][0]['text']
            cleaned = raw_text.replace('```json', '').replace('```', '').strip()

            print(f"   ⏳ Pacing: Waiting {REQUEST_DELAY} seconds to protect API quota...")
            time.sleep(REQUEST_DELAY)

            return json.loads(cleaned)

        except requests.exceptions.ConnectionError:
            print(f"   ⚠️ Connection dropped by Google (Attempt {attempt + 1}). Waiting 15s...")
            time.sleep(15)
        except Exception as e:
            print(f"   ⚠️ API Error (Attempt {attempt + 1}/{max_retries}): {e}")
            time.sleep(10)

    return None


def fetch_page_with_retry(url, headers, max_retries=3):
    """Handles SarkariResult's unstable servers (ConnectionResetError 10054)"""
    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=headers, timeout=20)
            response.raise_for_status()
            return response
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                print(f"   ⚠️ Website connection issue: Retrying in 5s... ({e})")
                time.sleep(5)
            else:
                print(f"   ❌ Failed to load website after {max_retries} attempts.")
                return None


# ==========================================
# 3. CORE SCRAPING ENGINE
# ==========================================
def process_category(category_name, max_items=15):
    target_url = URL_MAP[category_name]
    print(f"\n==============================================")
    print(f"🚀 STARTING SCRAPE FOR: {category_name}")
    print(f"==============================================")

    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

    response = fetch_page_with_retry(target_url, headers)
    if not response:
        return

    soup = BeautifulSoup(response.content, 'html.parser')
    post_div = soup.find('div', id='post')
    if not post_div:
        print("❌ Could not find the list on the page.")
        return

    target_links = post_div.find_all('a')[:max_items]

    collection_ref = db.collection('draft_vacancies')

    for index, a_tag in enumerate(target_links):
        job_url = urljoin(target_url, a_tag.get('href'))
        job_title = a_tag.get_text(strip=True)

        url_hash = hashlib.md5(job_url.encode()).hexdigest()
        doc_id = f"job_{url_hash[:10]}"

        draft_doc = collection_ref.document(doc_id).get()
        live_doc = db.collection('vacancies').document(doc_id).get()

        if draft_doc.exists or live_doc.exists:
            print(f"⏭️ [{index + 1}/{len(target_links)}] Skipped (Already in DB): {job_title}")
            continue

        print(f"⚙️ [{index + 1}/{len(target_links)}] Processing: {job_title}")

        try:
            job_page_response = fetch_page_with_retry(job_url, headers)
            if not job_page_response:
                continue

            job_soup = BeautifulSoup(job_page_response.content, 'html.parser')
            page_text = job_soup.get_text(separator=' ', strip=True)

            if category_name == "VACANCY":
                all_dates = re.findall(r'\d{2}[/-]\d{2}[/-]\d{4}', page_text)
                is_active = False
                current_date = datetime.now()

                for date_str in all_dates:
                    date_str = date_str.replace('-', '/')
                    if date_str.startswith("01/01/") or date_str.startswith("01/07/") or date_str.startswith("01/08/"):
                        continue
                    try:
                        parsed_date = datetime.strptime(date_str, "%d/%m/%Y")
                        if parsed_date >= current_date:
                            is_active = True
                            break
                    except ValueError:
                        continue

                if not is_active and re.search(r'(Exam|Mains|Tier|Interview|Admit Card).*?(Soon|Later|Notified)',
                                               page_text, re.IGNORECASE):
                    is_active = True

                if not is_active:
                    print(f"   ⏳ Skipped: Phase expired.")
                    continue

            exact_links = extract_exact_links(job_soup)

            schema = get_schema_prompt(category_name)
            job_data = call_gemini_api(schema, page_text[:8000], job_url)

            if not job_data:
                print(f"   ❌ AI failed to extract data.")
                continue

            job_data["id"] = doc_id
            job_data["links"] = exact_links
            job_data["sourceLink"] = job_url
            job_data["status"] = "DRAFT"
            job_data["scrapedAt"] = datetime.now().isoformat()

            collection_ref.document(doc_id).set(job_data)
            print(f"   ✅ Uploaded New Draft to Firebase!")

        except Exception as e:
            print(f"   ❌ Error on page {job_title}: {e}")


# ==========================================
# 4. EXECUTION MENU
# ==========================================
if __name__ == "__main__":

    # --- AUTOMATED MODE: called with arguments from GitHub Actions ---
    # Usage: python master_scraper.py VACANCY 10
    if len(sys.argv) >= 2:
        category_arg = sys.argv[1].upper()
        count_arg = int(sys.argv[2]) if len(sys.argv) >= 3 else 15

        if category_arg == "ALL":
            process_category("VACANCY", max_items=count_arg)
            process_category("ADMIT_CARD", max_items=count_arg)
            process_category("RESULT", max_items=count_arg)
            process_category("ANSWER_KEY", max_items=count_arg)
        elif category_arg in URL_MAP:
            process_category(category_arg, max_items=count_arg)
        else:
            print(f"Unknown category: {category_arg}. Use VACANCY, ADMIT_CARD, RESULT, ANSWER_KEY, or ALL")

    # --- INTERACTIVE MODE: run without arguments on your laptop ---
    else:
        print("Welcome to SarkariAutofill Master Scraper (Free Tier Optimized)")
        print("1. Run All Categories")
        print("2. Scrape Latest Jobs (Vacancies) Only")
        print("3. Scrape Admit Cards Only")
        print("4. Scrape Results Only")
        print("5. Scrape Answer Keys Only")
        print("6. Exit")

        choice = input("\nEnter your choice (1-6): ")

        if choice in ['1', '2', '3', '4', '5']:
            try:
                scan_count = int(input("How many items to scan per category? "))
            except ValueError:
                print("Invalid number. Defaulting to 15.")
                scan_count = 15

            if choice == '1':
                process_category("VACANCY", max_items=scan_count)
                process_category("ADMIT_CARD", max_items=scan_count)
                process_category("RESULT", max_items=scan_count)
                process_category("ANSWER_KEY", max_items=scan_count)
            elif choice == '2':
                process_category("VACANCY", max_items=scan_count)
            elif choice == '3':
                process_category("ADMIT_CARD", max_items=scan_count)
            elif choice == '4':
                process_category("RESULT", max_items=scan_count)
            elif choice == '5':
                process_category("ANSWER_KEY", max_items=scan_count)

    print("\n✨ Operations Completed! Check Firebase Drafts.")