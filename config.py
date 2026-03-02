import os
from pymongo import MongoClient

# RapidAPI Configuration for Resume Parser
RAPIDAPI_HOST = "ai-resume-parser3.p.rapidapi.com"
RAPIDAPI_KEY = os.getenv('RAPIDAPI_KEY')

# Resume Matcher API Configuration
RESUME_MATCHER_HOST = os.getenv('RESUME_MATCHER_HOST', 'resume-matcher-api.p.rapidapi.com')
RESUME_MATCHER_API_KEY = os.getenv('RESUME_MATCHER_API_KEY')
RESUME_MATCHER_ENDPOINT = os.getenv('RESUME_MATCHER_ENDPOINT', '/batch/match')

# Skills Parser API Configuration
SKILLS_PARSER_HOST = "skills-parser1.p.rapidapi.com"
SKILLS_PARSER_API_KEY = os.getenv('SKILLS_PARSER_API_KEY')

def get_database():
    """
    Get MongoDB database connection with proper error handling
    """
    MONGO_URI = os.getenv('MONGO_URI')
    
    # Connection options to fix SSL issues
    connection_options = {
        'serverSelectionTimeoutMS': 5000,
        'connectTimeoutMS': 10000,
        'socketTimeoutMS': 10000,
        'retryWrites': True,
    }
    
    # Add TLS options if using MongoDB Atlas
    if MONGO_URI and 'mongodb+srv://' in MONGO_URI:
        connection_options.update({
            'tls': True,
            'tlsAllowInvalidCertificates': True,  # For development
        })
    
    # Try cloud connection first
    if MONGO_URI:
        try:
            client = MongoClient(MONGO_URI, **connection_options)
            client.admin.command('ping')
            db = client.get_database()
            print(f"✅ MongoDB Atlas Connected: {db.name}")
            return client, db
        except Exception as e:
            print(f"⚠️  MongoDB Atlas connection failed: {str(e)}")
    
    # Fallback to local MongoDB
    try:
        local_uri = 'mongodb://localhost:27017/'
        client = MongoClient(local_uri, serverSelectionTimeoutMS=2000)
        client.admin.command('ping')
        db = client['resume_ats']
        print(f"✅ Local MongoDB Connected: {db.name}")
        return client, db
    except Exception as e:
        print(f"❌ All MongoDB connections failed: {str(e)}")
        return None, None
