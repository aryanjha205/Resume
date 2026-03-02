from flask import Flask, render_template, request, jsonify, session, redirect, url_for, send_from_directory
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from pymongo import MongoClient
from bson.objectid import ObjectId
from datetime import datetime, timedelta
import os
import re
import json
import requests
from dotenv import load_dotenv
import subprocess
import base64
import http.client
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from difflib import SequenceMatcher
import certifi

# Load environment variables from .env file
load_dotenv()
from config import RAPIDAPI_HOST, RAPIDAPI_KEY, RESUME_MATCHER_HOST, RESUME_MATCHER_API_KEY, RESUME_MATCHER_ENDPOINT, SKILLS_PARSER_HOST, SKILLS_PARSER_API_KEY
import secrets
# Heavy imports moved inside functions to speed up startup on Vercel

app = Flask(__name__)
app.secret_key = secrets.token_hex(32)
# Use /tmp for uploads on Vercel/Serverless as it's the only writable directory
app.config['UPLOAD_FOLDER'] = os.path.join('/tmp', 'uploads') if os.environ.get('VERCEL') or os.environ.get('AWS_LAMBDA_FUNCTION_NAME') else 'uploads'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(hours=24)

# Serve PWA files from root
@app.route('/manifest.json')
def serve_manifest():
    return send_from_directory('static', 'manifest.json')

@app.route('/sw.js')
def serve_sw():
    return send_from_directory('static', 'sw.js')

try:
    os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
except Exception as e:
    print(f"Warning: Could not create upload directory: {e}")

# Global error tracking for debugging
db_connection_error = None
latest_error = None

# MongoDB Configuration
MONGO_URI = os.getenv('MONGO_URI')

if not MONGO_URI:
    print("\n⚠ MongoDB Connection Error: MONGO_URI environment variable not set")
    # Use local MongoDB as fallback for development
    MONGO_URI = 'mongodb://localhost:27017/resume_ats'
    print(f"📍 Falling back to local MongoDB: {MONGO_URI}\n")

# MongoDB URI handling moved to explicit database selection for reliability

try:
    # Enhanced connection parameters to resolve SSL issues
    connection_params = {
        'serverSelectionTimeoutMS': 5000, # Reduced for faster failure/fallback
        'connectTimeoutMS': 10000,
        'socketTimeoutMS': 10000,
        'retryWrites': True,
    }
    
    # Add SSL/TLS parameters only for MongoDB Atlas connections
    if MONGO_URI and ('mongodb+srv://' in MONGO_URI or 'mongodb.net' in MONGO_URI):
        # certifi.where() is the most reliable way to get CA certs in serverless
        connection_params.update({
            'tls': True,
            'tlsCAFile': certifi.where(),
        })
    
    client = MongoClient(MONGO_URI, **connection_params)
    
    # Explicitly select the database name for consistency across environments
    db = client.get_database('resume_ats')
    
    # Collections
    users_collection = db.users
    jobs_collection = db.jobs
    applications_collection = db.applications
    assessments_collection = db.assessments
    notifications_collection = db.notifications
    
    print(f"✅ MongoDB Connected Successfully to: {db.name}\n")
    
except Exception as e:
    # Masked URI for safe logging in Vercel
    masked_uri = "NOT_SET"
    if MONGO_URI:
        try:
            parts = MONGO_URI.split('@')
            if len(parts) > 1:
                masked_uri = "mongodb+srv://****:****@" + parts[1]
            else:
                masked_uri = MONGO_URI[:20] + "..."
        except:
            masked_uri = "REDACTED"
            
    print(f"\n⚠ MongoDB Connection Fatal Error: {str(e)}")
    print(f"Attempted URI: {masked_uri}")
    db_connection_error = str(e)
    
    # Try local connection as fallback
    try:
        print("\n🔄 Attempting local MongoDB connection...")
        client = MongoClient('mongodb://localhost:27017/', serverSelectionTimeoutMS=2000)
        client.admin.command('ping')
        db = client['resume_ats']
        
        # Collections
        users_collection = db.users
        jobs_collection = db.jobs
        applications_collection = db.applications
        assessments_collection = db.assessments
        notifications_collection = db.notifications
        
        print(f"✅ Connected to local MongoDB: {db.name}\n")
    except Exception as local_error:
        print(f"❌ Local MongoDB also unavailable: {str(local_error)}")
        print("⚠️  Application will run but database operations will fail!\n")
        
        # Create mock collections for development
        class MockCollection:
            is_mock = True
            def find_one(self, *args, **kwargs):
                return None
            def find(self, *args, **kwargs):
                return []
            def insert_one(self, *args, **kwargs):
                return type('obj', (object,), {'inserted_id': 'mock_id'})()
            def update_one(self, *args, **kwargs):
                return type('obj', (object,), {'modified_count': 0})()
            def count_documents(self, *args, **kwargs):
                return 0
            def distinct(self, *args, **kwargs):
                return []
        
        users_collection = MockCollection()
        jobs_collection = MockCollection()
        applications_collection = MockCollection()
        assessments_collection = MockCollection()
        notifications_collection = MockCollection()
        db = None

# Hugging Face API Configuration
HF_API_TOKEN = os.environ.get('HF_API_TOKEN', '')
HF_API_URL = "https://api-inference.huggingface.co/models/facebook/bart-large-mnli"

ALLOWED_EXTENSIONS = {'pdf', 'docx', 'doc'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def extract_text_from_pdf(file_path):
    import PyPDF2
    text = ""
    try:
        with open(file_path, 'rb') as file:
            pdf_reader = PyPDF2.PdfReader(file)
            for page in pdf_reader.pages:
                text += page.extract_text() + "\n"
    except:
        pass
    return text

def extract_text_from_docx(file_path):
    import docx
    text = ""
    try:
        doc = docx.Document(file_path)
        for para in doc.paragraphs:
            text += para.text + "\n"
    except:
        pass
    return text

def extract_text_from_resume(file_path):
    if file_path.endswith('.pdf'):
        return extract_text_from_pdf(file_path)
    elif file_path.endswith('.docx') or file_path.endswith('.doc'):
        return extract_text_from_docx(file_path)
    return ""

def parse_resume_with_rapidapi(file_path):
    """
    Parse resume using RapidAPI AI Resume Parser
    Supports PDF and DOCX files
    
    Returns:
        dict: Parsed resume data or error information
    """
    try:
        if not RAPIDAPI_KEY:
            return {
                'success': False,
                'error': 'RAPIDAPI_KEY not configured. Please set RAPIDAPI_KEY in .env file'
            }
        
        # Read file and encode to base64
        with open(file_path, 'rb') as file:
            file_content = file.read()
            file_base64 = base64.b64encode(file_content).decode('utf-8')
        
        # Determine media type
        media_type = 'application/pdf' if file_path.endswith('.pdf') else 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        
        # Prepare payload
        payload = json.dumps({
            'image_base64': file_base64,
            'media_type': media_type,
            'include_raw_text': False
        })
        
        # Make API request
        conn = http.client.HTTPSConnection(RAPIDAPI_HOST)
        headers = {
            'x-rapidapi-key': RAPIDAPI_KEY,
            'x-rapidapi-host': RAPIDAPI_HOST,
            'Content-Type': 'application/json'
        }
        
        conn.request('POST', '/parse/base64', payload, headers)
        response = conn.getresponse()
        data = response.read()
        
        result = json.loads(data.decode('utf-8'))
        
        if response.status == 200:
            return {
                'success': True,
                'data': result
            }
        else:
            return {
                'success': False,
                'error': result.get('message', 'Failed to parse resume'),
                'status_code': response.status
            }
            
    except FileNotFoundError:
        return {
            'success': False,
            'error': f'File not found: {file_path}'
        }
    except json.JSONDecodeError:
        return {
            'success': False,
            'error': 'Invalid response from API'
        }
    except Exception as e:
        return {
            'success': False,
            'error': str(e)
        }
    finally:
        try:
            conn.close()
        except:
            pass

def extract_skills_with_ai(resume_text):
    """Extract skills using pattern matching and NLP"""
    skills = set()
    
    # Common technical skills patterns
    tech_skills = [
        'python', 'java', 'javascript', 'c++', 'c#', 'ruby', 'php', 'swift', 'kotlin', 'go',
        'react', 'angular', 'vue', 'node.js', 'express', 'django', 'flask', 'spring', 'asp.net',
        'html', 'css', 'sass', 'bootstrap', 'tailwind', 'jquery',
        'sql', 'mysql', 'postgresql', 'mongodb', 'redis', 'elasticsearch', 'oracle',
        'aws', 'azure', 'gcp', 'docker', 'kubernetes', 'jenkins', 'git', 'ci/cd',
        'machine learning', 'deep learning', 'nlp', 'computer vision', 'tensorflow', 'pytorch', 'scikit-learn',
        'agile', 'scrum', 'jira', 'confluence', 'rest api', 'graphql', 'microservices',
        'leadership', 'communication', 'teamwork', 'problem solving', 'project management',
        'data analysis', 'excel', 'powerbi', 'tableau', 'analytics'
    ]
    
    resume_lower = resume_text.lower()
    
    for skill in tech_skills:
        if skill in resume_lower:
            skills.add(skill.title())
    
    # Extract education
    education_patterns = [
        r'b\.?tech', r'b\.?e\.?', r'm\.?tech', r'm\.?s\.?', r'phd', r'mba', r'bba',
        r'bachelor', r'master', r'computer science', r'engineering', r'information technology'
    ]
    
    for pattern in education_patterns:
        matches = re.findall(pattern, resume_lower)
        if matches:
            skills.add(pattern.replace(r'\.?', '.').upper())
    
    # Extract years of experience
    exp_matches = re.findall(r'(\d+)\+?\s*(?:years?|yrs?)', resume_lower)
    if exp_matches:
        skills.add(f"{max(map(int, exp_matches))}+ Years Experience")
    
    # Use Hugging Face for additional skill extraction (if available)
    if HF_API_TOKEN:
        try:
            candidate_labels = ['programming', 'databases', 'cloud computing', 'project management', 'leadership']
            headers = {"Authorization": f"Bearer {HF_API_TOKEN}"}
            payload = {
                "inputs": resume_text[:1000],
                "parameters": {"candidate_labels": candidate_labels}
            }
            response = requests.post(HF_API_URL, headers=headers, json=payload, timeout=10)
            if response.status_code == 200:
                result = response.json()
                if isinstance(result, dict) and 'labels' in result:
                    for label, score in zip(result['labels'], result['scores']):
                        if score > 0.5:
                            skills.add(label.title())
        except:
            pass
    
    return list(skills)

def match_resume_with_rapidapi(resume_text, jd_text, top_k_skills=10, expected_years=0):
    """
    Match resume with job description using Resume Matcher API
    Returns match score and top matching skills
    """
    try:
        if not RESUME_MATCHER_API_KEY:
            return None
        
        payload = json.dumps({
            'matches': [{
                'resume_text': resume_text[:5000],  # Limit to 5000 chars
                'jd_text': jd_text[:5000],
                'top_k_skills': top_k_skills,
                'expected_years': expected_years,
                'use_semantic_scoring': True
            }],
            'webhook_url': ''
        })
        
        headers = {
            'x-rapidapi-key': RESUME_MATCHER_API_KEY,
            'x-rapidapi-host': RESUME_MATCHER_HOST,
            'Content-Type': 'application/json'
        }
        
        conn = http.client.HTTPSConnection(RESUME_MATCHER_HOST)
        conn.request('POST', RESUME_MATCHER_ENDPOINT, payload, headers)
        response = conn.getresponse()
        data = response.read()
        
        result = json.loads(data.decode('utf-8'))
        
        if response.status == 200:
            return {
                'success': True,
                'data': result
            }
        else:
            return None
            
    except Exception as e:
        print(f"Resume Matcher API error: {str(e)}")
        return None
    finally:
        try:
            conn.close()
        except:
            pass

def parse_skills_from_jd(job_description):
    """
    Parse skills from job description using Skills Parser API
    
    Args:
        job_description (str): The job description text to parse
        
    Returns:
        dict: Parsed skills data with success status and skills list
    """
    try:
        if not SKILLS_PARSER_API_KEY:
            return {
                'success': False,
                'error': 'SKILLS_PARSER_API_KEY not configured. Please set SKILLS_PARSER_API_KEY in .env file'
            }
        
        # Prepare payload with job description
        payload = json.dumps({
            "job_description": job_description
        })
        
        headers = {
            'x-rapidapi-key': SKILLS_PARSER_API_KEY,
            'x-rapidapi-host': SKILLS_PARSER_HOST,
            'Content-Type': 'application/json'
        }
        
        # Make API request
        conn = http.client.HTTPSConnection(SKILLS_PARSER_HOST)
        conn.request("POST", "/parse_skills", payload, headers)
        
        res = conn.getresponse()
        data = res.read()
        
        result = json.loads(data.decode("utf-8"))
        
        if res.status == 200:
            return {
                'success': True,
                'data': result
            }
        else:
            return {
                'success': False,
                'error': result.get('message', 'Failed to parse skills from job description'),
                'status_code': res.status
            }
            
    except json.JSONDecodeError:
        return {
            'success': False,
            'error': 'Invalid response from API'
        }
    except Exception as e:
        return {
            'success': False,
            'error': str(e)
        }
    finally:
        try:
            conn.close()
        except:
            pass

def extract_skills_from_job_description(job_description):
    """
    Extract skills from job description using pattern matching (fallback method)
    
    Args:
        job_description (str): The job description text
        
    Returns:
        list: Extracted skills
    """
    if not job_description:
        return []
    
    skills = set()
    jd_lower = job_description.lower()
    
    # Common technical skills and keywords
    tech_skills_keywords = {
        'programming': ['python', 'java', 'javascript', 'c++', 'c#', 'ruby', 'php', 'swift', 'kotlin', 'go', 'rust', 'r', 'scala', 'perl'],
        'web': ['react', 'angular', 'vue', 'node.js', 'express', 'django', 'flask', 'spring', 'asp.net', 'laravel', 'rails'],
        'frontend': ['html', 'css', 'sass', 'bootstrap', 'tailwind', 'jquery', 'typescript'],
        'backend': ['rest api', 'graphql', 'microservices', 'websocket', 'grpc'],
        'databases': ['sql', 'mysql', 'postgresql', 'mongodb', 'redis', 'elasticsearch', 'oracle', 'cassandra', 'dynamodb'],
        'cloud': ['aws', 'azure', 'gcp', 'google cloud', 'heroku', 'docker', 'kubernetes', 'ci/cd', 'jenkins', 'gitlab'],
        'ml_ai': ['machine learning', 'deep learning', 'nlp', 'computer vision', 'tensorflow', 'pytorch', 'scikit-learn', 'ai', 'artificial intelligence'],
        'soft': ['agile', 'scrum', 'jira', 'confluence', 'leadership', 'communication', 'teamwork', 'problem solving'],
        'data': ['data analysis', 'excel', 'power bi', 'tableau', 'analytics', 'powerbi', 'pandas', 'numpy', 'matplotlib'],
        'devops': ['git', 'linux', 'unix', 'devops', 'terraform', 'ansible', 'jenkins', 'docker', 'kubernetes'],
    }
    
    # Check for skills
    for category, skill_list in tech_skills_keywords.items():
        for skill in skill_list:
            if skill in jd_lower:
                skills.add(skill.title())
    
    # Extract years of experience requirement
    exp_matches = re.findall(r'(\d+)\+?\s*(?:years?|yrs?)\s+(?:of\s+)?(?:experience|exp)', jd_lower)
    if exp_matches:
        skills.add(f"{exp_matches[0]}+ Years Experience")
    
    # Extract education requirements
    education_keywords = ['b.tech', 'b.e.', 'm.tech', 'm.s.', 'phd', 'mba', 'bba', 'bachelor', 'master']
    for edu in education_keywords:
        if edu in jd_lower:
            skills.add(edu.upper())
    
    return list(skills)

def calculate_match_score(candidate_skills, job_requirements, resume_text='', jd_text=''):
    """Calculate match score using Resume Matcher API or fallback to cosine similarity"""
    if not candidate_skills or not job_requirements:
        return 0.0
    
    # Try Resume Matcher API first if we have resume and JD text
    if resume_text and jd_text:
        api_result = match_resume_with_rapidapi(resume_text, jd_text)
        if api_result and api_result.get('success'):
            try:
                matches = api_result.get('data', {}).get('matches', [])
                if matches and isinstance(matches, list) and len(matches) > 0:
                    match_data = matches[0]
                    if 'match_score' in match_data:
                        return float(match_data['match_score']) * 100
            except:
                pass
    
    # Fallback to cosine similarity
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    import numpy as np
    
    candidate_text = ' '.join([str(s).lower() for s in candidate_skills])
    job_text = ' '.join([str(r).lower() for r in job_requirements])
    
    vectorizer = TfidfVectorizer()
    try:
        tfidf_matrix = vectorizer.fit_transform([candidate_text, job_text])
        similarity = cosine_similarity(tfidf_matrix[0:1], tfidf_matrix[1:2])
        return float(similarity[0][0] * 100)
    except:
        # Fallback to simple matching
        candidate_set = set(candidate_text.split())
        job_set = set(job_text.split())
        if not job_set:
            return 0.0
        matches = len(candidate_set.intersection(job_set))
        return (matches / len(job_set)) * 100

def calculate_text_similarity(text_a, text_b):
    """Calculate similarity between two text blocks"""
    if not text_a or not text_b:
        return 0.0
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    vectorizer = TfidfVectorizer(stop_words='english')
    try:
        tfidf_matrix = vectorizer.fit_transform([text_a.lower(), text_b.lower()])
        similarity = cosine_similarity(tfidf_matrix[0:1], tfidf_matrix[1:2])
        return float(similarity[0][0] * 100)
    except:
        return 0.0

def extract_years_experience(resume_text):
    """Extract max years of experience from resume text"""
    if not resume_text:
        return 0
    matches = re.findall(r'(\d+(?:\.\d+)?)\+?\s*(?:years?|yrs?)', resume_text.lower())
    if not matches:
        return 0
    try:
        return int(max(float(m) for m in matches))
    except:
        return 0

def normalize_skill_for_matching(skill):
    """
    Normalize a skill string for robust matching
    Handles: case, whitespace, punctuation, version numbers, abbreviations
    """
    if not skill:
        return ""
    
    # Convert to lowercase and strip
    s = str(skill).lower().strip()
    
    # Expand common abbreviations
    abbreviation_map = {
        'js': 'javascript',
        'ts': 'typescript',
        'py': 'python',
        'cpp': 'c++',
        'csharp': 'c#',
        'c#': 'c#',
        'db': 'database',
        'ml': 'machine learning',
        'ai': 'artificial intelligence',
        'nlp': 'natural language processing',
        'cv': 'computer vision',
        'rest api': 'rest',
        'api': 'rest',
        'nosql': 'nosql',
        'sql': 'sql',
        'html': 'html',
        'css': 'css',
        'k8s': 'kubernetes',
        'k8': 'kubernetes',
    }
    
    # Check if it matches an abbreviation
    for abbr, expanded in abbreviation_map.items():
        if s == abbr or s.startswith(abbr + ' ') or s.endswith(' ' + abbr):
            s = s.replace(abbr, expanded)
    
    # Normalize dots and hyphens
    s = s.replace('.', ' ').replace('-', ' ')
    
    # Remove version numbers
    s = re.sub(r'\s*[\d.]+[a-z]?\+?\s*', ' ', s)
    
    # Remove special characters except space
    s = re.sub(r'[^\w\s+#]', '', s)
    
    # Normalize whitespace
    s = re.sub(r'\s+', ' ', s).strip()
    
    return s

def calculate_skills_percentage(job_skills, resume_skills):
    """
    Calculate the percentage of job required skills present in resume using robust matching
    
    Args:
        job_skills (list): List of skills required in the job posting
        resume_skills (list): List of skills extracted from the resume
        
    Returns:
        dict: Contains percentage match, matched skills, missing skills, and details
    """
    if not job_skills:
        return {
            'percentage': 100.0,
            'matched_count': 0,
            'total_skills': 0,
            'matched_skills': [],
            'missing_skills': [],
            'match_details': 'No skills required'
        }
    
    if not resume_skills:
        return {
            'percentage': 0.0,
            'matched_count': 0,
            'total_skills': len(job_skills),
            'matched_skills': [],
            'missing_skills': [str(s) for s in job_skills],
            'match_details': f'No skills found in resume. Required: {", ".join(str(s) for s in job_skills)}'
        }
    
    # Normalize all skills
    job_skills_normalized = [normalize_skill_for_matching(skill) for skill in job_skills]
    resume_skills_normalized = [normalize_skill_for_matching(skill) for skill in resume_skills]
    
    # Remove empty entries
    job_skills_normalized = [s for s in job_skills_normalized if s]
    resume_skills_normalized = [s for s in resume_skills_normalized if s]
    
    matched_skills = []
    missing_skills = []
    
    # Check each job skill against resume skills
    for i, job_skill in enumerate(job_skills_normalized):
        skill_found = False
        original_skill = str(job_skills[i])  # Keep original for display
        
        # Exact match
        if job_skill in resume_skills_normalized:
            matched_skills.append(original_skill)
            skill_found = True
        else:
            # Fuzzy matching with similarity
            for resume_skill in resume_skills_normalized:
                similarity = SequenceMatcher(None, job_skill, resume_skill).ratio()
                
                # Match if high similarity or substring
                if similarity >= 0.75 or (len(job_skill) > 3 and job_skill in resume_skill) or (len(resume_skill) > 3 and resume_skill in job_skill):
                    matched_skills.append(original_skill)
                    skill_found = True
                    break
        
        if not skill_found:
            missing_skills.append(original_skill)
    
    # Calculate percentage
    matched_count = len(matched_skills)
    total_skills = len(job_skills_normalized)
    percentage = (matched_count / total_skills * 100) if total_skills > 0 else 0.0
    
    # Categorize percentage
    if percentage >= 80:
        match_category = "Excellent"
    elif percentage >= 60:
        match_category = "Good"
    elif percentage >= 40:
        match_category = "Fair"
    elif percentage >= 20:
        match_category = "Poor"
    else:
        match_category = "Critical Gap"
    
    return {
        'percentage': round(percentage, 2),
        'matched_count': matched_count,
        'total_skills': total_skills,
        'matched_skills': matched_skills,
        'missing_skills': missing_skills,
        'match_category': match_category,
        'match_details': f'{matched_count} of {total_skills} skills matched ({match_category}). Missing: {", ".join(missing_skills) if missing_skills else "None"}'
    }




def parse_required_experience(experience_text):
    """Parse required experience from job criteria"""
    if not experience_text:
        return 0
    lowered = experience_text.lower()
    if 'fresher' in lowered or 'no experience' in lowered:
        return 0
    matches = re.findall(r'(\d+(?:\.\d+)?)', lowered)
    if not matches:
        return 0
    try:
        return int(max(float(m) for m in matches))
    except:
        return 0

def extract_education_level(resume_text):
    """Estimate highest education level from resume text"""
    if not resume_text:
        return 0
    text = resume_text.lower()
    levels = {
        'phd': 5,
        'doctorate': 5,
        'm.tech': 4,
        'mtech': 4,
        'm.s': 4,
        'ms': 4,
        'mba': 4,
        'master': 4,
        'b.tech': 3,
        'btech': 3,
        'b.e': 3,
        'be': 3,
        'b.sc': 3,
        'bsc': 3,
        'bachelor': 3,
        'associate': 2,
        'diploma': 2,
        'high school': 1,
        'secondary': 1
    }
    detected = 0
    for key, level in levels.items():
        if key in text:
            detected = max(detected, level)
    return detected

def parse_required_education(education_text):
    """Parse required education level from job criteria"""
    if not education_text:
        return 0
    return extract_education_level(education_text)

def send_email(to_email, subject, message, html_message=None):
    """Send email using Python's smtplib"""
    if not to_email:
        return False, 'Missing recipient email'
    
    user = os.getenv('EMAIL_USER')
    password = os.getenv('EMAIL_PASS')
    from_email = os.getenv('EMAIL_FROM') or user
    
    if not user or not password:
        return False, 'EMAIL_USER or EMAIL_PASS not configured'
        
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = from_email
        msg['To'] = to_email
        
        # Attach plain text
        msg.attach(MIMEText(message, 'plain'))
        
        # Attach HTML content if provided
        if html_message:
            msg.attach(MIMEText(html_message, 'html'))
            
        # Gmail SMTP server configuration
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(user, password)
        server.send_message(msg)
        server.quit()
        
        return True, None
    except Exception as e:
        return False, str(e)

# Alias for backward compatibility if needed, but safer to use the new Python version
def send_email_via_nodemailer(to_email, subject, message, html_message=None):
    return send_email(to_email, subject, message, html_message)

def generate_professional_email(candidate_name, job_title, company, status):
    """Generate professional HTML email with job details"""
    templates = {
        'shortlisted': {
            'subject': f'Congratulations! You Have Been Shortlisted for {job_title}',
            'text': f'''Dear {candidate_name},

Congratulations! 🎉

We are pleased to inform you that your application for the position of {job_title} at {company} has been shortlisted for the next round.

Our recruitment team will contact you soon with further details regarding the next steps in the selection process.

Thank you for your interest in joining {company}.

Best regards,
Recruitment Team
{company}''',
            'html': f'''<!DOCTYPE html>
<html>
<head>
    <style>
        body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
        .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
        .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 30px; text-align: center; border-radius: 8px 8px 0 0; }}
        .content {{ background: #f9f9f9; padding: 30px; border: 1px solid #ddd; }}
        .highlight {{ background: #e8f5e9; border-left: 4px solid #4caf50; padding: 15px; margin: 20px 0; }}
        .footer {{ background: #333; color: white; padding: 20px; text-align: center; border-radius: 0 0 8px 8px; font-size: 12px; }}
        h1 {{ margin: 0; font-size: 24px; }}
        .emoji {{ font-size: 40px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="emoji">🎉</div>
            <h1>Congratulations!</h1>
        </div>
        <div class="content">
            <p>Dear <strong>{candidate_name}</strong>,</p>
            <p>We are pleased to inform you that your application for the position of <strong>{job_title}</strong> at <strong>{company}</strong> has been <strong>shortlisted</strong> for the next round.</p>
            <div class="highlight">
                <strong>Position:</strong> {job_title}<br>
                <strong>Company:</strong> {company}<br>
                <strong>Status:</strong> Shortlisted ✅
            </div>
            <p>Our recruitment team will contact you soon with further details regarding the next steps in the selection process.</p>
            <p>Thank you for your interest in joining {company}. We look forward to speaking with you!</p>
        </div>
        <div class="footer">
            <p>Best regards,<br><strong>Recruitment Team</strong><br>{company}</p>
        </div>
    </div>
</body>
</html>'''
        },
        'rejected': {
            'subject': f'Application Status Update - {job_title} at {company}',
            'text': f'''Dear {candidate_name},

Thank you for your interest in the {job_title} position at {company} and for taking the time to apply.

After careful consideration of all applications, we regret to inform you that we will not be moving forward with your application at this time.

We received many strong applications, and the decision was difficult. We encourage you to apply for other positions at {company} that match your skills and experience.

We wish you the best in your job search and future career endeavors.

Best wishes,
Recruitment Team
{company}''',
            'html': f'''<!DOCTYPE html>
<html>
<head>
    <style>
        body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
        .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
        .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 30px; text-align: center; border-radius: 8px 8px 0 0; }}
        .content {{ background: #f9f9f9; padding: 30px; border: 1px solid #ddd; }}
        .highlight {{ background: #fff3e0; border-left: 4px solid #ff9800; padding: 15px; margin: 20px 0; }}
        .footer {{ background: #333; color: white; padding: 20px; text-align: center; border-radius: 0 0 8px 8px; font-size: 12px; }}
        h1 {{ margin: 0; font-size: 24px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>Application Status Update</h1>
        </div>
        <div class="content">
            <p>Dear <strong>{candidate_name}</strong>,</p>
            <p>Thank you for your interest in the <strong>{job_title}</strong> position at <strong>{company}</strong> and for taking the time to apply.</p>
            <div class="highlight">
                <strong>Position:</strong> {job_title}<br>
                <strong>Company:</strong> {company}<br>
                <strong>Status:</strong> Not Shortlisted
            </div>
            <p>After careful consideration of all applications, we regret to inform you that we will not be moving forward with your application at this time.</p>
            <p>We received many strong applications, and the decision was difficult. We encourage you to apply for other positions at {company} that match your skills and experience.</p>
            <p>We wish you the best in your job search and future career endeavors.</p>
        </div>
        <div class="footer">
            <p>Best wishes,<br><strong>Recruitment Team</strong><br>{company}</p>
        </div>
    </div>
</body>
</html>'''
        },
        'hired': {
            'subject': f'Congratulations! Job Offer - {job_title} at {company}',
            'text': f'''Dear {candidate_name},

Congratulations! 🎉🎊

We are thrilled to inform you that you have been selected for the position of {job_title} at {company}!

Your skills, experience, and performance throughout the selection process have impressed us, and we believe you will be a valuable addition to our team.

Our HR team will contact you within the next 2-3 business days with:
• Formal offer letter
• Compensation details
• Joining date and onboarding information
• Required documentation

We are excited to welcome you to {company}!

Warm regards,
Recruitment Team
{company}''',
            'html': f'''<!DOCTYPE html>
<html>
<head>
    <style>
        body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
        .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
        .header {{ background: linear-gradient(135deg, #11998e 0%, #38ef7d 100%); color: white; padding: 30px; text-align: center; border-radius: 8px 8px 0 0; }}
        .content {{ background: #f9f9f9; padding: 30px; border: 1px solid #ddd; }}
        .highlight {{ background: #e8f5e9; border-left: 4px solid #4caf50; padding: 15px; margin: 20px 0; }}
        .footer {{ background: #333; color: white; padding: 20px; text-align: center; border-radius: 0 0 8px 8px; font-size: 12px; }}
        .emoji {{ font-size: 50px; }}
        h1 {{ margin: 0; font-size: 28px; }}
        ul {{ text-align: left; margin: 15px 0; }}
        li {{ margin: 8px 0; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="emoji">🎉🎊</div>
            <h1>Congratulations!</h1>
            <p style="margin-top: 10px; font-size: 18px;">You're Hired!</p>
        </div>
        <div class="content">
            <p>Dear <strong>{candidate_name}</strong>,</p>
            <p>We are thrilled to inform you that you have been <strong>selected</strong> for the position of <strong>{job_title}</strong> at <strong>{company}</strong>!</p>
            <div class="highlight">
                <strong>Position:</strong> {job_title}<br>
                <strong>Company:</strong> {company}<br>
                <strong>Status:</strong> HIRED ✅
            </div>
            <p>Your skills, experience, and performance throughout the selection process have impressed us, and we believe you will be a valuable addition to our team.</p>
            <p><strong>Next Steps:</strong></p>
            <p>Our HR team will contact you within the next 2-3 business days with:</p>
            <ul>
                <li>Formal offer letter</li>
                <li>Compensation details</li>
                <li>Joining date and onboarding information</li>
                <li>Required documentation</li>
            </ul>
            <p>We are excited to welcome you to {company}!</p>
        </div>
        <div class="footer">
            <p>Warm regards,<br><strong>Recruitment Team</strong><br>{company}</p>
        </div>
    </div>
</body>
</html>'''
        },
        'selected': {
            'subject': f'Congratulations! You Have Been Selected for {job_title}',
            'text': f'''Dear {candidate_name},

Congratulations! 🎉

We are pleased to inform you that you have been selected for the position of {job_title} at {company}!

Your application and assessment results were impressive. Our recruitment team will contact you soon with the next steps.

Thank you for your interest in joining {company}.

Best regards,
Recruitment Team
{company}''',
            'html': f'''<!DOCTYPE html>
<html>
<head>
    <style>
        body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; }}
        .container {{ max-width: 600px; margin: 0 auto; padding: 20px; }}
        .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 30px; text-align: center; border-radius: 8px 8px 0 0; }}
        .content {{ background: #f9f9f9; padding: 30px; border: 1px solid #ddd; }}
        .highlight {{ background: #e8f5e9; border-left: 4px solid #4caf50; padding: 15px; margin: 20px 0; }}
        .footer {{ background: #333; color: white; padding: 20px; text-align: center; border-radius: 0 0 8px 8px; font-size: 12px; }}
        h1 {{ margin: 0; font-size: 24px; }}
        .emoji {{ font-size: 40px; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="emoji">🎉</div>
            <h1>Congratulations!</h1>
        </div>
        <div class="content">
            <p>Dear <strong>{candidate_name}</strong>,</p>
            <p>We are pleased to inform you that you have been <strong>selected</strong> for the position of <strong>{job_title}</strong> at <strong>{company}</strong>!</p>
            <div class="highlight">
                <strong>Position:</strong> {job_title}<br>
                <strong>Company:</strong> {company}<br>
                <strong>Status:</strong> Selected ✅
            </div>
            <p>Your application and assessment results were impressive. Our recruitment team will contact you soon with the next steps.</p>
            <p>Thank you for your interest in joining {company}.</p>
        </div>
        <div class="footer">
            <p>Best regards,<br><strong>Recruitment Team</strong><br>{company}</p>
        </div>
    </div>
</body>
</html>'''
        }
    }
    
    return templates.get(status, templates['shortlisted'])

def generate_assessment_questions(job_title, required_skills):
    """Generate 15 MCQ questions based on job requirements"""
    questions = []
    
    # Predefined question templates for common skills
    question_templates = {
        'python': [
            {
                'question': 'What is the output of: print(type([]) is list)?',
                'options': ['True', 'False', 'Error', 'None'],
                'correct': 0
            },
            {
                'question': 'Which keyword is used to define a function in Python?',
                'options': ['function', 'def', 'func', 'define'],
                'correct': 1
            },
            {
                'question': 'What does the len() function return for a dictionary?',
                'options': ['Number of values', 'Number of keys', 'Total items', 'Size in bytes'],
                'correct': 1
            }
        ],
        'javascript': [
            {
                'question': 'What does "use strict" do in JavaScript?',
                'options': ['Enables strict mode', 'Imports a library', 'Defines a constant', 'Creates a class'],
                'correct': 0
            },
            {
                'question': 'Which method adds an element to the end of an array?',
                'options': ['append()', 'push()', 'add()', 'insert()'],
                'correct': 1
            }
        ],
        'sql': [
            {
                'question': 'Which SQL clause is used to filter records?',
                'options': ['FILTER', 'WHERE', 'HAVING', 'SELECT'],
                'correct': 1
            },
            {
                'question': 'What does INNER JOIN return?',
                'options': ['All records', 'Matching records from both tables', 'Left table records', 'Right table records'],
                'correct': 1
            }
        ],
        'react': [
            {
                'question': 'What is JSX in React?',
                'options': ['JavaScript XML', 'Java Syntax Extension', 'JSON Extension', 'JavaScript Extra'],
                'correct': 0
            },
            {
                'question': 'Which hook is used for side effects in React?',
                'options': ['useState', 'useEffect', 'useContext', 'useReducer'],
                'correct': 1
            }
        ],
        'default': [
            {
                'question': 'What is the time complexity of Binary Search?',
                'options': ['O(n)', 'O(log n)', 'O(n log n)', 'O(1)'],
                'correct': 1
            },
            {
                'question': 'Which data structure works on FIFO principle?',
                'options': ['Stack', 'Queue', 'Tree', 'Graph'],
                'correct': 1
            },
            {
                'question': 'Which of the following is NOT a Java primitive data type?',
                'options': ['int', 'float', 'String', 'char'],
                'correct': 2
            },
            {
                'question': 'What is the default value of boolean in Java?',
                'options': ['true', 'false', '0', 'null'],
                'correct': 1
            },
            {
                'question': 'Which sorting algorithm has best average time complexity?',
                'options': ['Bubble Sort', 'Selection Sort', 'Merge Sort', 'Insertion Sort'],
                'correct': 2
            },
            {
                'question': 'What is normalization in DBMS?',
                'options': ['Deleting data', 'Organizing data to reduce redundancy', 'Creating backup', 'Encrypting database'],
                'correct': 1
            },
            {
                'question': 'Which SQL command is used to remove a table?',
                'options': ['DELETE', 'REMOVE', 'DROP', 'TRUNCATE'],
                'correct': 2
            },
            {
                'question': 'What is the full form of JVM?',
                'options': ['Java Variable Machine', 'Java Virtual Machine', 'Java Verified Machine', 'Joint Virtual Machine'],
                'correct': 1
            },
            {
                'question': 'Which data structure is used in recursion?',
                'options': ['Queue', 'Stack', 'Array', 'Tree'],
                'correct': 1
            },
            {
                'question': 'What is the worst-case time complexity of Quick Sort?',
                'options': ['O(n log n)', 'O(log n)', 'O(n²)', 'O(n)'],
                'correct': 2
            },
            {
                'question': 'Which protocol is used to transfer web pages?',
                'options': ['FTP', 'HTTP', 'SMTP', 'TCP'],
                'correct': 1
            },
            {
                'question': 'What is the primary key in DBMS?',
                'options': ['Duplicate key', 'Unique identifier for each record', 'Foreign key', 'Normal key'],
                'correct': 1
            },
            {
                'question': 'Which of the following is not an OOP principle?',
                'options': ['Encapsulation', 'Abstraction', 'Compilation', 'Polymorphism'],
                'correct': 2
            },
            {
                'question': 'What does RAM stand for?',
                'options': ['Read Access Memory', 'Random Access Memory', 'Run Access Memory', 'Rapid Access Memory'],
                'correct': 1
            },
            {
                'question': 'Which traversal technique uses Queue?',
                'options': ['DFS', 'BFS', 'Inorder', 'Postorder'],
                'correct': 1
            }
        ]
    }
    
    # Collect questions based on required skills
    for skill in required_skills:
        skill_lower = skill.lower()
        for key in question_templates:
            if key in skill_lower:
                questions.extend(question_templates[key])
                break
    
    # Add default questions to reach 15
    questions.extend(question_templates['default'])
    
    # Ensure we have exactly 15 questions
    if len(questions) > 15:
        questions = questions[:15]
    elif len(questions) < 15:
        # Duplicate some questions if needed
        while len(questions) < 15:
            questions.append(question_templates['default'][len(questions) % len(question_templates['default'])])
    
    # Add question numbers
    for i, q in enumerate(questions):
        q['id'] = i + 1
    
    return questions

def create_notification(user_id, message, notification_type='info'):
    """Create a notification for a user"""
    notification = {
        'user_id': str(user_id),
        'message': message,
        'type': notification_type,
        'read': False,
        'created_at': datetime.utcnow()
    }
    notifications_collection.insert_one(notification)

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/register', methods=['POST'])
def register():
    data = request.json
    
    email = data.get('email', '').lower().strip()
    password = data.get('password', '')
    name = data.get('name', '').strip()
    role = data.get('role', 'candidate')
    
    if not email or not password or not name:
        return jsonify({'success': False, 'message': 'All fields are required'}), 400
    
    try:
        # Check if email exists - this is where it might fail if DB is down
        if users_collection.find_one({'email': email}):
            return jsonify({'success': False, 'message': 'Email already registered. Please login instead.'}), 400
        
        user = {
            'email': email,
            'password': generate_password_hash(password),
            'name': name,
            'role': role,
            'created_at': datetime.utcnow()
        }
        
        if role == 'candidate':
            user['skills'] = []
            user['resume_path'] = None
        
        result = users_collection.insert_one(user)
        
        session['user_id'] = str(result.inserted_id)
        session['user_role'] = role
        session['user_name'] = name
        session.permanent = True
        
        return jsonify({
            'success': True,
            'message': 'Registration successful',
            'user': {
                'id': str(result.inserted_id),
                'name': name,
                'email': email,
                'role': role
            }
        })
    except Exception as e:
        error_msg = str(e)
        print(f"Registration error: {error_msg}")
        # Provide a more helpful error for common Atlas issues
        if "Authentication failed" in error_msg:
            friendly_msg = "Database authentication failed. Please check your username and password."
        elif "timeout" in error_msg.lower():
            friendly_msg = "Database connection timed out. Please check your Network Access (IP whitelist) in Atlas."
        else:
            friendly_msg = f"Database error: {error_msg}"
        return jsonify({'success': False, 'message': friendly_msg}), 500

@app.route('/api/login', methods=['POST'])
def login():
    data = request.json
    
    email = data.get('email', '').lower().strip()
    password = data.get('password', '')
    
    if not email or not password:
        return jsonify({'success': False, 'message': 'Email and password required'}), 400
    
    try:
        user = users_collection.find_one({'email': email})
        
        if not user:
            return jsonify({'success': False, 'message': 'No account found with this email. Please register first.'}), 401
        
        if not check_password_hash(user['password'], password):
            return jsonify({'success': False, 'message': 'Incorrect password. Please try again.'}), 401
        
        session['user_id'] = str(user['_id'])
        session['user_role'] = user['role']
        session['user_name'] = user['name']
        session.permanent = True
        
        return jsonify({
            'success': True,
            'message': 'Login successful',
            'user': {
                'id': str(user['_id']),
                'name': user['name'],
                'email': user['email'],
                'role': user['role']
            }
        })
    except Exception as e:
        error_msg = str(e)
        print(f"Login error: {error_msg}")
        if "Authentication failed" in error_msg:
            friendly_msg = "Database authentication failed. Please check your username and password."
        elif "timeout" in error_msg.lower():
            friendly_msg = "Database connection timed out. Please check your Network Access (IP whitelist) in Atlas."
        else:
            friendly_msg = f"Database error: {error_msg}"
        return jsonify({'success': False, 'message': friendly_msg}), 500

@app.route('/api/logout', methods=['POST'])
def logout():
    session.clear()
    return jsonify({'success': True, 'message': 'Logged out successfully'})

@app.route('/api/check-auth', methods=['GET'])
def check_auth():
    if 'user_id' in session:
        return jsonify({
            'authenticated': True,
            'user': {
                'id': session['user_id'],
                'name': session.get('user_name', 'Unknown'),
                'role': session.get('user_role', 'candidate')
            }
        })
    return jsonify({'authenticated': False})

@app.route('/api/upload-resume', methods=['POST'])
def upload_resume():
    if 'user_id' not in session or session.get('user_role') != 'candidate':
        return jsonify({'success': False, 'message': 'Please login as a candidate first'}), 401
    
    if 'resume' not in request.files:
        return jsonify({'success': False, 'message': 'No file uploaded'}), 400
    
    file = request.files['resume']
    
    if file.filename == '':
        return jsonify({'success': False, 'message': 'No file selected'}), 400
    
    if not allowed_file(file.filename):
        return jsonify({'success': False, 'message': 'Invalid file type. Only PDF and DOCX allowed'}), 400
    
    filename = secure_filename(f"{session['user_id']}_{datetime.utcnow().timestamp()}_{file.filename}")
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    file.save(file_path)
    
    # Extract text and skills
    resume_text = extract_text_from_resume(file_path)
    skills = extract_skills_with_ai(resume_text)
    
    # Update user profile
    users_collection.update_one(
        {'_id': ObjectId(session['user_id'])},
        {'$set': {
            'resume_path': file_path,
            'skills': skills,
            'resume_text': resume_text,
            'updated_at': datetime.utcnow()
        }}
    )
    
    return jsonify({
        'success': True,
        'message': 'Resume uploaded and processed successfully',
        'skills': skills
    })

@app.route('/api/parse-resume', methods=['POST'])
def parse_resume():
    """
    Parse resume file using RapidAPI AI Resume Parser
    Requires authenticated user
    File should be in request.files under 'resume' key
    """
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    
    if 'resume' not in request.files:
        return jsonify({'success': False, 'message': 'No file uploaded'}), 400
    
    file = request.files['resume']
    
    if file.filename == '':
        return jsonify({'success': False, 'message': 'No file selected'}), 400
    
    if not allowed_file(file.filename):
        return jsonify({'success': False, 'message': 'Invalid file type. Only PDF and DOCX allowed'}), 400
    
    try:
        # Save file temporarily
        filename = secure_filename(f"{session['user_id']}_{datetime.utcnow().timestamp()}_{file.filename}")
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        file.save(file_path)
        
        # Parse resume using RapidAPI
        parse_result = parse_resume_with_rapidapi(file_path)
        
        if not parse_result['success']:
            return jsonify({
                'success': False,
                'message': parse_result['error']
            }), 400
        
        parsed_data = parse_result['data']
        
        # Extract key information from parsed resume
        response_data = {
            'success': True,
            'message': 'Resume parsed successfully',
            'parsed_resume': parsed_data,
            'file_path': file_path
        }
        
        return jsonify(response_data), 200
        
    except Exception as e:
        return jsonify({
            'success': False,
            'message': f'Error parsing resume: {str(e)}'
        }), 500

@app.route('/api/candidate/jobs', methods=['GET'])
def get_jobs_for_candidate():
    if 'user_id' not in session or session.get('user_role') != 'candidate':
        return jsonify({'success': False, 'message': 'Please login as a candidate first'}), 401
    
    user = users_collection.find_one({'_id': ObjectId(session['user_id'])})
    candidate_skills = user.get('skills', [])
    
    jobs = list(jobs_collection.find({'status': 'active'}))
    
    # Calculate match scores
    jobs_with_scores = []
    for job in jobs:
        job['_id'] = str(job['_id'])
        job['recruiter_id'] = str(job['recruiter_id'])
        
        # Check if already applied
        application = applications_collection.find_one({
            'job_id': job['_id'],
            'candidate_id': session['user_id']
        })
        
        job['applied'] = application is not None
        job['application_status'] = application.get('status') if application else None
        
        # Calculate match score using improved skill matching
        required_skills = job.get('required_skills', [])
        skills_match = calculate_skills_percentage(required_skills, candidate_skills)
        job['match_score'] = skills_match.get('percentage', 0)
        job['matched_skills'] = skills_match.get('matched_skills', [])
        
        jobs_with_scores.append(job)
    
    # Sort by match score
    jobs_with_scores.sort(key=lambda x: x['match_score'], reverse=True)
    
    return jsonify({'success': True, 'jobs': jobs_with_scores})

@app.route('/api/candidate/apply', methods=['POST'])
def apply_for_job():
    # Better authentication check with debugging
    if 'user_id' not in session:
        return jsonify({'success': False, 'message': 'Please login first'}), 401
    
    if 'user_role' not in session:
        return jsonify({'success': False, 'message': 'Session error: role not found. Please login again'}), 401
        
    if session.get('user_role') != 'candidate':
        return jsonify({'success': False, 'message': 'Only candidates can apply for jobs'}), 403
    
    data = request.json
    job_id = data.get('job_id')
    
    if not job_id:
        return jsonify({'success': False, 'message': 'Job ID required'}), 400
    
    job = jobs_collection.find_one({'_id': ObjectId(job_id)})
    if not job:
        return jsonify({'success': False, 'message': 'Job not found'}), 404
    
    # Check if already applied
    existing_application = applications_collection.find_one({
        'job_id': job_id,
        'candidate_id': session['user_id']
    })
    
    if existing_application:
        return jsonify({'success': False, 'message': 'Already applied for this job'}), 400
    
    user = users_collection.find_one({'_id': ObjectId(session['user_id'])})
    
    if not user.get('resume_path'):
        return jsonify({'success': False, 'message': 'Please upload your resume first'}), 400
    
    # Calculate match score using improved skill matching
    candidate_skills = user.get('skills', [])
    required_skills = job.get('required_skills', [])
    skills_match = calculate_skills_percentage(required_skills, candidate_skills)
    match_score = skills_match.get('percentage', 0)
    
    # Auto-shortlist if match score >= 60%
    status = 'shortlisted' if match_score >= 60 else 'applied'
    
    application = {
        'job_id': job_id,
        'candidate_id': session['user_id'],
        'candidate_name': user['name'],
        'candidate_email': user['email'],
        'candidate_skills': candidate_skills,
        'match_score': round(match_score, 2),
        'skills_percentage': match_score,
        'matched_skills': skills_match.get('matched_skills', []),
        'status': status,
        'applied_at': datetime.utcnow(),
        'assessment_completed': False,
        'assessment_score': None
    }
    
    result = applications_collection.insert_one(application)
    
    # Create notification for recruiter
    if status == 'shortlisted':
        create_notification(
            str(job['recruiter_id']),
            f"Candidate {user['name']} has been auto-shortlisted for {job['title']} with {match_score:.1f}% match",
            'success'
        )
    
    return jsonify({
        'success': True,
        'message': f'Application submitted successfully. {("You have been auto-shortlisted!" if status == "shortlisted" else "")}',
        'application_id': str(result.inserted_id),
        'status': status,
        'match_score': round(match_score, 2)
    })

@app.route('/api/candidate/applications', methods=['GET'])
def get_candidate_applications():
    if 'user_id' not in session or session.get('user_role') != 'candidate':
        return jsonify({'success': False, 'message': 'Please login as a candidate first'}), 401
    
    applications = list(applications_collection.find({'candidate_id': session['user_id']}))
    
    applications_with_jobs = []
    for app in applications:
        app['_id'] = str(app['_id'])
        job = jobs_collection.find_one({'_id': ObjectId(app['job_id'])})
        if job:
            app['job_title'] = job['title']
            app['company'] = job.get('company', 'N/A')
        applications_with_jobs.append(app)
    
    return jsonify({'success': True, 'applications': applications_with_jobs})

@app.route('/api/candidate/assessment/<application_id>', methods=['GET'])
def get_assessment(application_id):
    if 'user_id' not in session or session['user_role'] != 'candidate':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    
    application = applications_collection.find_one({
        '_id': ObjectId(application_id),
        'candidate_id': session['user_id']
    })
    
    if not application:
        return jsonify({'success': False, 'message': 'Application not found'}), 404
    
    if application.get('assessment_completed'):
        return jsonify({'success': False, 'message': 'Assessment already completed'}), 400
    
    if application['status'] != 'shortlisted':
        return jsonify({'success': False, 'message': 'Only shortlisted candidates can take assessment'}), 400
    
    # Get or create assessment
    assessment = assessments_collection.find_one({'application_id': application_id})
    
    if not assessment:
        job = jobs_collection.find_one({'_id': ObjectId(application['job_id'])})
        questions = generate_assessment_questions(job['title'], job.get('required_skills', []))
        
        assessment = {
            'application_id': application_id,
            'candidate_id': session['user_id'],
            'job_id': application['job_id'],
            'questions': questions,
            'answers': [],
            'score': None,
            'completed': False,
            'started_at': datetime.utcnow(),
            'expires_at': datetime.utcnow() + timedelta(minutes=15)
        }
        
        assessments_collection.insert_one(assessment)
    
    # Remove correct answers before sending to frontend
    questions_for_frontend = []
    for q in assessment['questions']:
        questions_for_frontend.append({
            'id': q['id'],
            'question': q['question'],
            'options': q['options']
        })
    
    return jsonify({
        'success': True,
        'assessment': {
            'application_id': application_id,
            'questions': questions_for_frontend,
            'total_questions': len(questions_for_frontend),
            'time_limit': 15
        }
    })

@app.route('/api/candidate/submit-assessment', methods=['POST'])
def submit_assessment():
    if 'user_id' not in session or session.get('user_role') != 'candidate':
        return jsonify({'success': False, 'message': 'Please login as a candidate first'}), 401
    
    data = request.json
    application_id = data.get('application_id')
    answers = data.get('answers', {})
    
    assessment = assessments_collection.find_one({
        'application_id': application_id,
        'candidate_id': session['user_id']
    })
    
    if not assessment:
        return jsonify({'success': False, 'message': 'Assessment not found'}), 404
    
    if assessment.get('completed'):
        return jsonify({'success': False, 'message': 'Assessment already completed'}), 400
    
    # Calculate score
    correct_answers = 0
    total_questions = len(assessment['questions'])
    
    for question in assessment['questions']:
        question_id = str(question['id'])
        if question_id in answers:
            if int(answers[question_id]) == question['correct']:
                correct_answers += 1
    
    score = (correct_answers / total_questions) * 100
    
    # Update assessment
    assessments_collection.update_one(
        {'_id': assessment['_id']},
        {'$set': {
            'answers': answers,
            'score': round(score, 2),
            'completed': True,
            'completed_at': datetime.utcnow()
        }}
    )
    
    # Update application
    new_status = 'selected' if score >= 60 else 'rejected'
    
    applications_collection.update_one(
        {'_id': ObjectId(application_id)},
        {'$set': {
            'assessment_completed': True,
            'assessment_score': round(score, 2),
            'status': new_status,
            'updated_at': datetime.utcnow()
        }}
    )
    
    # Get application and job details for notification
    application = applications_collection.find_one({'_id': ObjectId(application_id)})
    job = jobs_collection.find_one({'_id': ObjectId(application['job_id'])})
    
    # Create notification for recruiter
    if new_status == 'selected':
        create_notification(
            str(job['recruiter_id']),
            f"Candidate {application['candidate_name']} passed the assessment for {job['title']} with {score:.1f}%",
            'success'
        )
    
    return jsonify({
        'success': True,
        'message': f'Assessment submitted successfully',
        'score': round(score, 2),
        'status': new_status,
        'passed': score >= 60
    })

@app.route('/api/recruiter/jobs', methods=['GET'])
def get_recruiter_jobs():
    if 'user_id' not in session or session['user_role'] != 'recruiter':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    
    jobs = list(jobs_collection.find({'recruiter_id': ObjectId(session['user_id'])}))
    
    for job in jobs:
        job['_id'] = str(job['_id'])
        job['recruiter_id'] = str(job['recruiter_id'])
        
        # Count applications
        total_applications = applications_collection.count_documents({'job_id': job['_id']})
        hired = applications_collection.count_documents({'job_id': job['_id'], 'status': 'hired'})
        rejected = applications_collection.count_documents({'job_id': job['_id'], 'status': 'rejected'})
        
        job['total_applications'] = total_applications
        job['hired_count'] = hired
        job['rejected_count'] = rejected
    
    return jsonify({'success': True, 'jobs': jobs})

@app.route('/api/recruiter/post-job', methods=['POST'])
def post_job():
    if 'user_id' not in session or session['user_role'] != 'recruiter':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    
    data = request.json
    
    job = {
        'recruiter_id': ObjectId(session['user_id']),
        'recruiter_name': session['user_name'],
        'title': data.get('title', '').strip(),
        'company': data.get('company', '').strip(),
        'description': data.get('description', '').strip(),
        'required_skills': [s.strip() for s in data.get('required_skills', []) if s.strip()],
        'experience': data.get('experience', '').strip(),
        'education': data.get('education', '').strip(),
        'salary': data.get('salary', '').strip(),
        'location': data.get('location', '').strip(),
        'status': 'active',
        'created_at': datetime.utcnow()
    }
    
    if not job['title'] or not job['description'] or not job['required_skills']:
        return jsonify({'success': False, 'message': 'Title, description, and required skills are mandatory'}), 400
    
    result = jobs_collection.insert_one(job)
    
    return jsonify({
        'success': True,
        'message': 'Job posted successfully',
        'job_id': str(result.inserted_id)
    })

@app.route('/api/recruiter/screen-application', methods=['POST'])
def screen_application():
    if 'user_id' not in session or session['user_role'] != 'recruiter':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401

    data = request.json
    application_id = data.get('application_id')

    if not application_id:
        return jsonify({'success': False, 'message': 'Application ID required'}), 400

    application = applications_collection.find_one({'_id': ObjectId(application_id)})
    if not application:
        return jsonify({'success': False, 'message': 'Application not found'}), 404

    job = jobs_collection.find_one({'_id': ObjectId(application['job_id'])})
    if not job or str(job.get('recruiter_id')) != session['user_id']:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403

    candidate = users_collection.find_one({'_id': ObjectId(application['candidate_id'])})
    if not candidate:
        return jsonify({'success': False, 'message': 'Candidate not found'}), 404

    resume_text = candidate.get('resume_text', '')
    candidate_skills = candidate.get('skills', [])

    required_skills = job.get('required_skills', [])
    
    # If required_skills is empty or incomplete, extract from job description
    if not required_skills or len(required_skills) < 2:
        job_description = job.get('description', '')
        if job_description:
            # Try API first
            skills_parse_result = parse_skills_from_jd(job_description)
            if skills_parse_result.get('success'):
                parsed_data = skills_parse_result.get('data', {})
                extracted_skills = parsed_data.get('skills', []) or parsed_data.get('required_skills', [])
                if extracted_skills:
                    required_skills = extracted_skills
            
            # Fallback to local extraction if API didn't work
            if not required_skills:
                required_skills = extract_skills_from_job_description(job_description)
    
    # If still no skills, try extracting from job skills directly
    if not required_skills and job.get('skills'):
        required_skills = job.get('skills', [])
    
    # Log for debugging
    print(f"\n📊 Screening Debug Info:")
    print(f"   Job Required Skills: {required_skills}")
    print(f"   Candidate Skills: {candidate_skills}")
    
    # Calculate skills percentage using our improved matching
    skills_percentage_data = calculate_skills_percentage(required_skills, candidate_skills)
    skills_percentage = skills_percentage_data.get('percentage', 0.0)
    print(f"   Skills Match Result: {skills_percentage}%")
    print(f"   Matched Skills: {skills_percentage_data.get('matched_skills')}")
    print(f"   Missing Skills: {skills_percentage_data.get('missing_skills')}\n")
    
    # Use skills percentage and text similarity for overall score
    desc_score = calculate_text_similarity(resume_text, job.get('description', ''))
    # Skills percentage (70%) + Description similarity (30%)
    overall_score = round((0.7 * skills_percentage) + (0.3 * desc_score), 2)

    required_experience = parse_required_experience(job.get('experience', ''))
    candidate_experience = extract_years_experience(resume_text)
    experience_ok = candidate_experience >= required_experience

    required_education = parse_required_education(job.get('education', ''))
    candidate_education = extract_education_level(resume_text)
    education_ok = candidate_education >= required_education if required_education else True

    is_selected = overall_score >= 60 and experience_ok and education_ok
    screening_status = 'Selected' if is_selected else 'Not Selected'

    applications_collection.update_one(
        {'_id': ObjectId(application_id)},
        {'$set': {
            'screening_result': screening_status,
            'screening_score': overall_score,
            'skills_percentage': skills_percentage_data.get('percentage'),
            'matched_skills': skills_percentage_data.get('matched_skills'),
            'missing_skills': skills_percentage_data.get('missing_skills'),
            'skills_match_category': skills_percentage_data.get('match_category'),
            'screened_at': datetime.utcnow(),
            'status': 'selected' if is_selected else 'rejected'
        }}
    )

    # Get professional email template
    candidate_email = application.get('candidate_email') or candidate.get('email')
    candidate_name = application.get('candidate_name', 'Candidate')
    job_title = job.get('title', 'Position')
    company = job.get('company', 'Our Company')
    status = 'selected' if is_selected else 'rejected'
    
    email_template = generate_professional_email(candidate_name, job_title, company, status)
    email_sent, email_error = send_email_via_nodemailer(
        candidate_email,
        email_template['subject'],
        email_template['text'],
        email_template['html']
    )

    if not email_sent:
        return jsonify({
            'success': False,
            'message': 'Screening completed but email failed to send',
            'screening_status': screening_status,
            'screening_score': overall_score,
            'email_error': email_error
        }), 500

    return jsonify({
        'success': True,
        'message': 'Screening completed and email sent',
        'screening_status': screening_status,
        'screening_score': overall_score,
        'skills_percentage': skills_percentage_data.get('percentage'),
        'skills_match_category': skills_percentage_data.get('match_category'),
        'matched_skills': skills_percentage_data.get('matched_skills'),
        'missing_skills': skills_percentage_data.get('missing_skills'),
        'skills_details': skills_percentage_data.get('match_details')
    })

@app.route('/api/recruiter/applications/<job_id>', methods=['GET'])
def get_job_applications(job_id):
    if 'user_id' not in session or session['user_role'] != 'recruiter':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    
    job = jobs_collection.find_one({
        '_id': ObjectId(job_id),
        'recruiter_id': ObjectId(session['user_id'])
    })
    
    if not job:
        return jsonify({'success': False, 'message': 'Job not found'}), 404
    
    applications = list(applications_collection.find({'job_id': job_id}))
    
    for app in applications:
        app['_id'] = str(app['_id'])
    
    # Sort by match score
    applications.sort(key=lambda x: x.get('match_score', 0), reverse=True)
    
    return jsonify({'success': True, 'applications': applications})

@app.route('/api/recruiter/update-application', methods=['POST'])
def update_application_status():
    if 'user_id' not in session or session['user_role'] != 'recruiter':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    
    data = request.json
    application_id = data.get('application_id')
    new_status = data.get('status')
    
    if new_status not in ['shortlisted', 'rejected', 'hired']:
        return jsonify({'success': False, 'message': 'Invalid status'}), 400
    
    application = applications_collection.find_one({'_id': ObjectId(application_id)})
    
    if not application:
        return jsonify({'success': False, 'message': 'Application not found'}), 404
    
    # Verify recruiter owns this job
    job = jobs_collection.find_one({
        '_id': ObjectId(application['job_id']),
        'recruiter_id': ObjectId(session['user_id'])
    })
    
    if not job:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 403
    
    applications_collection.update_one(
        {'_id': ObjectId(application_id)},
        {'$set': {
            'status': new_status,
            'updated_at': datetime.utcnow()
        }}
    )

    # Email candidate on status update with professional template
    candidate_email = application.get('candidate_email')
    if not candidate_email:
        candidate_user = users_collection.find_one({'_id': ObjectId(application['candidate_id'])})
        candidate_email = candidate_user.get('email') if candidate_user else None
        
    if candidate_email:
        candidate_name = application.get('candidate_name', 'Candidate')
        job_title = job.get('title', 'Position')
        company = job.get('company', 'Our Company')
        
        email_template = generate_professional_email(candidate_name, job_title, company, new_status)
        send_email_via_nodemailer(
            candidate_email,
            email_template['subject'],
            email_template['text'],
            email_template['html']
        )
    
    # Create notification for candidate
    create_notification(
        application['candidate_id'],
        f"Your application for {job['title']} has been {new_status}",
        'info'
    )
    
    return jsonify({'success': True, 'message': f'Application {new_status} successfully'})

@app.route('/api/recruiter/notifications', methods=['GET'])
def get_recruiter_notifications():
    if 'user_id' not in session or session['user_role'] != 'recruiter':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    
    notifications = list(notifications_collection.find(
        {'user_id': session['user_id']}
    ).sort('created_at', -1).limit(20))
    
    for notif in notifications:
        notif['_id'] = str(notif['_id'])
        notif['created_at'] = notif['created_at'].isoformat()
    
    return jsonify({'success': True, 'notifications': notifications})

@app.route('/api/recruiter/mark-notification-read', methods=['POST'])
def mark_notification_read():
    if 'user_id' not in session or session['user_role'] != 'recruiter':
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401
    
    data = request.json
    notification_id = data.get('notification_id')
    
    notifications_collection.update_one(
        {'_id': ObjectId(notification_id), 'user_id': session['user_id']},
        {'$set': {'read': True}}
    )
    
    return jsonify({'success': True})

@app.route('/api/stats', methods=['GET'])
def get_stats():
    try:
        global latest_error
        # These calls will trigger a connection attempt if it hasn't happened yet
        total_jobs = jobs_collection.count_documents({'status': 'active'})
        total_candidates = users_collection.count_documents({'role': 'candidate'})
        total_applications = applications_collection.count_documents({})
        companies = jobs_collection.distinct('company')
        is_connected = True
    except Exception as e:
        error_msg = str(e)
        print(f"Stats fetch error: {error_msg}")
        total_jobs = 0
        total_candidates = 0
        total_applications = 0
        companies = []
        is_connected = False
        latest_error = error_msg
    
    mongo_env_set = os.environ.get('MONGO_URI') is not None
    
    return jsonify({
        'success': True,
        'stats': {
            'total_jobs': total_jobs,
            'total_candidates': total_candidates,
            'total_applications': total_applications,
            'total_companies': len([c for c in companies if c]),
            'database_connected': is_connected,
            'mongo_uri_set': mongo_env_set,
            'error_hint': latest_error if not is_connected else None
        }
    })

# Debug endpoint for testing skill matching
@app.route('/api/debug/test-skills', methods=['POST'])
def test_skills():
    """Debug endpoint to test skill matching"""
    data = request.json
    job_skills = data.get('job_skills', [])
    resume_skills = data.get('resume_skills', [])
    
    result = calculate_skills_percentage(job_skills, resume_skills)
    
    return jsonify({
        'success': True,
        'input': {
            'job_skills': job_skills,
            'resume_skills': resume_skills
        },
        'result': result
    })

if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    app.run(debug=False, host='0.0.0.0', port=port)