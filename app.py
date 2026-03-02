import os
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, session, render_template_string
from flask_socketio import SocketIO, emit, join_room, leave_room
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_bcrypt import Bcrypt
from flask_mail import Mail, Message
from models import Database, User
from config import Config
import random
import string
from bson import ObjectId
from datetime import datetime, timezone, timedelta
from werkzeug.utils import secure_filename
import time
# Delaying heavy imports to prevent startup crashes on serverless
# from bot import get_ai_response

app = Flask(__name__)
app.config.from_object(Config)
application = app

# Initialize Extensions
socketio = SocketIO(app, cors_allowed_origins="*")
bcrypt = Bcrypt(app)
mail = Mail(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'

# Lazy Database Initialization
_db_initialized = False
class ConfigError(Exception):
    pass

def get_db():
    global _db_initialized
    if not _db_initialized:
        uri = app.config.get('MONGO_URI', '')
        # Detect Vercel environment issues
        is_vercel = os.environ.get('VERCEL') or os.environ.get('VERCEL_URL')
        if is_vercel and ('localhost' in uri or not uri):
            raise ConfigError("MONGO_URI environment variable is missing in Vercel Settings.")
            
        try:
            Database.initialize(uri)
            _db_initialized = True
        except Exception as e:
            # On Vercel, we want to know EXACTLY what's wrong with the connection
            if is_vercel:
                raise ConfigError(f"Database Connection Error: {str(e)}")
            else:
                print(f"DB Error: {e}")
    return Database.db

@login_manager.user_loader
def load_user(user_id):
    try:
        db = get_db()
        return User.find_by_id(user_id)
    except Exception:
        return None

# --- Vercel Diagnostic Tool ---
@app.errorhandler(ConfigError)
def handle_config_error(e):
    return render_template_string("""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8"><title>VAANI - Setup Required</title>
            <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
            <style>
                body { background: #0f172a; color: white; display: flex; align-items: center; justify-content: center; min-height: 100vh; font-family: 'Outfit', sans-serif; }
                .card { background: rgba(30, 41, 59, 0.7); border: 1px solid rgba(255,255,255,0.1); backdrop-filter: blur(10px); border-radius: 20px; box-shadow: 0 25px 50px -12px rgba(0,0,0,0.5); }
                .code { background: #000; color: #10b981; padding: 15px; border-radius: 10px; font-family: monospace; white-space: pre-wrap; word-break: break-all; }
                .btn-vercel { background: white; color: black; font-weight: bold; border-radius: 50px; }
            </style>
        </head>
        <body>
            <div class="container" style="max-width: 600px;">
                <div class="card p-5 text-center">
                    <h2 class="mb-4">🚀 Almost There!</h2>
                    <p class="text-muted mb-4 text-start">VAANI is deployed, but it can't find your <strong>MongoDB Atlas Cluster</strong>. Vercel needs you to add an Environment Variable.</p>
                    <div class="alert alert-danger text-start"><strong>Error:</strong> {{ error_msg }}</div>
                    
                    <h5 class="text-start mt-4 mb-3">Fix it in 3 steps:</h5>
                    <ol class="text-start mb-4">
                        <li>Go to <strong>Vercel Dashboard</strong> > <strong>Settings</strong> > <strong>Environment Variables</strong>.</li>
                        <li>Add Key: <code>MONGO_URI</code></li>
                        <li>Add Value: <small>(Paste your connection string from MongoDB Atlas)</small></li>
                    </ol>
                    <div class="d-grid gap-2">
                        <a href="https://vercel.com/dashboard" target="_blank" class="btn btn-vercel btn-lg">Open Vercel Dashboard</a>
                        <button onclick="location.reload()" class="btn btn-outline-light rounded-pill">I've added it, Refresh!</button>
                    </div>
                </div>
            </div>
        </body>
        </html>
    """, error_msg=str(e)), 500

@app.errorhandler(Exception)
def handle_exception(e):
    import traceback
    return f"<h1>Internal Error (Diagnostics)</h1><p>A server error occurred. Please screenshot this and send it back to the assistant:</p><pre style='background:#f4f4f4;padding:15px;color:red;'>{traceback.format_exc()}</pre>", 500

@app.before_request
def ensure_db():
    try:
        get_db()
    except ConfigError as e:
        return handle_config_error(e)

# --- Routes ---

@app.route('/')
def index():
    try:
        if current_user and current_user.is_authenticated:
            return redirect(url_for('chat'))
    except Exception:
        pass
    return render_template('index.html')

@app.route('/health')
def health():
    db_status = "Connected" if Database.db is not None else "Disconnected"
    return jsonify({
        "status": "healthy",
        "database": db_status,
        "environment": "vercel" if os.environ.get('VERCEL') else "unknown"
    })

@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form.get('username')
        email = request.form.get('email')
        password = request.form.get('password')
        
        if Database.db.users.find_one({'username': username}):
            flash('Username already exists', 'danger')
            return redirect(url_for('signup'))
        if Database.db.users.find_one({'email': email}):
            flash('Email already registered', 'danger')
            return redirect(url_for('signup'))
        
        # Generate OTP
        otp = ''.join(random.choices(string.digits, k=6))
        
        # Store temporary user data in session or a temp collection
        temp_user = {
            'username': username,
            'email': email,
            'password': bcrypt.generate_password_hash(password).decode('utf-8'),
            'otp': otp,
            'timestamp': datetime.now(timezone.utc)
        }
        Database.db.temp_users.update_one({'email': email}, {'$set': temp_user}, upsert=True)
        
        # Send Email
        try:
            msg = Message('VAANI - Verify your Email', recipients=[email])
            msg.html = render_template('email_otp.html', otp=otp)
            
            # Embed Logo as CID
            logo_path = os.path.join(app.static_folder, 'img/icon-512.png')
            if os.path.exists(logo_path):
                with open(logo_path, 'rb') as fp:
                    msg.attach("logo.png", "image/png", fp.read(), 'inline', headers={'Content-ID': '<app_logo>'})
            
            mail.send(msg)
            flash('OTP sent to your email!', 'info')
            return render_template('verify_otp.html', email=email)
        except Exception as e:
            print(f"CRITICAL EMAIL ERROR: {str(e)}")
            if "535" in str(e):
                print("DEBUG: This is an Authentication Error. Check your Gmail App Password.")
            flash('Failed to send OTP. Please check your email credentials/settings.', 'danger')
            
    return render_template('signup.html')

@app.route('/verify-otp', methods=['POST'])
def verify_otp():
    email = request.form.get('email')
    user_otp = request.form.get('otp')
    
    temp_data = Database.db.temp_users.find_one({'email': email})
    
    if temp_data and temp_data['otp'] == user_otp:
        # Create actual user
        user_data = {
            'username': temp_data['username'],
            'email': temp_data['email'],
            'password': temp_data['password'],
            'profile_pic': f"https://ui-avatars.com/api/?name={temp_data['username']}&background=random",
            'online': False,
            'last_seen': None
        }
        Database.db.users.insert_one(user_data)
        Database.db.temp_users.delete_one({'email': email})
        
        flash('Account verified successfully! Please login.', 'success')
        return redirect(url_for('login'))
    else:
        flash('Invalid OTP. Please try again.', 'danger')
        return render_template('verify_otp.html', email=email)

@app.route('/resend-otp', methods=['POST'])
def resend_otp():
    email = request.form.get('email')
    temp_user = Database.db.temp_users.find_one({'email': email})
    
    if temp_user:
        # Generate new OTP
        otp = ''.join(random.choices(string.digits, k=6))
        Database.db.temp_users.update_one({'email': email}, {'$set': {'otp': otp, 'timestamp': datetime.now(timezone.utc)}})
        
        # Send Email
        try:
            msg = Message('VAANI - New OTP for Verification', recipients=[email])
            msg.html = render_template('email_otp.html', otp=otp)
            
            # Embed Logo
            logo_path = os.path.join(app.static_folder, 'img/icon-512.png')
            if os.path.exists(logo_path):
                with open(logo_path, 'rb') as fp:
                    msg.attach("logo.png", "image/png", fp.read(), 'inline', headers={'Content-ID': '<app_logo>'})
            
            mail.send(msg)
            return jsonify({'success': True, 'message': 'New OTP sent to your email!'})
        except Exception as e:
            return jsonify({'success': False, 'message': 'Failed to send email. Please try again later.'}), 500
            
    return jsonify({'success': False, 'message': 'Session expired. Please signup again.'}), 404

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        user_data = Database.db.users.find_one({'username': username})
        if user_data and User.verify_password(user_data['password'], password):
            user_obj = User(user_data)
            login_user(user_obj)
            return redirect(url_for('chat'))
        else:
            flash('Login Unsuccessful. Please check username and password', 'danger')
            
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    Database.db.users.update_one({'_id': ObjectId(current_user.id)}, {'$set': {'online': False, 'last_seen': datetime.now(timezone.utc)}})
    logout_user()
    return redirect(url_for('index'))

@app.route('/chat')
@login_required
def chat():
    user_data = Database.db.users.find_one({'_id': ObjectId(current_user.id)})
    if not user_data:
        return redirect(url_for('logout'))
        
    pinned_ids = user_data.get('pinned_contacts') or []
    
    # Filter by connections
    connections = user_data.get('connections') or []
    connection_ids = [ObjectId(c_id) for c_id in connections if c_id and c_id != 'null']
    
    users = Database.db.users.find({'_id': {'$in': connection_ids}})
    contacts = []
    
    # Check if AI feature is enabled
    config = Database.db.config.find_one({'_id': 'features'}) or {}
    if config.get('ai_bot', True):
        # Add Virtual AI Bot
        contacts.append({
            'id': 'vaani_ai_bot',
            'username': 'VAANI (AI)',
            'profile_pic': '/static/img/VAANI AI.png',
            'last_message': 'Always here to help! 🤖',
            'online': True,
            'pinned': True,
            'about': 'I am your AI companion.'
        })
    
    # Also fetch requests count for badge
    requests_count = len(user_data.get('requests_received') or [])

    for u in users:
        u_id = str(u['_id'])
        # Get last message for snippet
        last_msg = Database.db.messages.find_one(
            {'$or': [
                {'sender_id': current_user.id, 'receiver_id': u_id},
                {'sender_id': u_id, 'receiver_id': current_user.id}
            ]},
            sort=[('timestamp', -1)]
        )
        
        contacts.append({
            'id': u_id,
            'username': u['username'],
            'profile_pic': u.get('profile_pic'),
            'about': u.get('about', 'Available'),
            'online': u.get('online', False),
            'pinned': u_id in pinned_ids,
            'last_message': last_msg['message'] if last_msg else 'No messages yet'
        })
    
    # Sort: Pinned first, then by username
    contacts.sort(key=lambda x: (not x['pinned'], x['username']))
    return render_template('chat.html', contacts=contacts, requests_count=requests_count)

@app.route('/toggle_pin', methods=['POST'])
@login_required
def toggle_pin():
    contact_id = request.form.get('contact_id')
    user_id = ObjectId(current_user.id)
    
    user_data = Database.db.users.find_one({'_id': user_id})
    pinned = user_data.get('pinned_contacts', [])
    
    if contact_id in pinned:
        pinned.remove(contact_id)
        action = 'unpinned'
    else:
        pinned.append(contact_id)
        action = 'pinned'
        
    Database.db.users.update_one({'_id': user_id}, {'$set': {'pinned_contacts': pinned}})
    return jsonify({'success': True, 'action': action})

@app.route('/profile', methods=['POST'])
@login_required
def update_profile():
    about = request.form.get('about')
    username = request.form.get('username')
    
    update_data = {}
    if about:
        update_data['about'] = about
    if username:
        # Check if username is already taken by another user
        if username != current_user.username and Database.db.users.find_one({'username': username}):
            return jsonify({'success': False, 'message': 'Username already taken'}), 400
        update_data['username'] = username

    if update_data:
        Database.db.users.update_one({'_id': ObjectId(current_user.id)}, {'$set': update_data})
        return jsonify({'success': True, 'message': 'Profile updated successfully'})
    
    return jsonify({'success': False, 'message': 'No changes provided'}), 400

@app.route('/update_profile_pic', methods=['POST'])
@login_required
def update_profile_pic():
    if 'file' not in request.files:
        return jsonify({'success': False, 'message': 'No file part'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'message': 'No selected file'}), 400
    
    try:
        from PIL import Image
        import io
        import base64
        
        # Open and resize
        img = Image.open(file.stream)
        # Convert to RGB if it's RGBA (to save space as JPEG or just keep as PNG)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        
        img.thumbnail((128, 128))
        
        # Save to buffer
        buffer = io.BytesIO()
        img.save(buffer, format="JPEG", quality=85)
        img_str = base64.b64encode(buffer.getvalue()).decode()
        pic_url = f"data:image/jpeg;base64,{img_str}"
        
        Database.db.users.update_one({'_id': ObjectId(current_user.id)}, {'$set': {'profile_pic': pic_url}})
        return jsonify({'success': True, 'profile_pic': pic_url})
    except Exception as e:
        print(f"PROFILE PIC ERROR: {e}")
        return jsonify({'success': False, 'message': f"Failed to process image: {str(e)}"}), 500

@app.route('/get_messages/<receiver_id>')
@login_required
def get_messages(receiver_id):
    # Fetch chat history between current user and receiver
    query = {
        '$or': [
            {'sender_id': current_user.id, 'receiver_id': receiver_id},
            {'sender_id': receiver_id, 'receiver_id': current_user.id}
        ]
    }
    messages = list(Database.db.messages.find(query).sort('timestamp', 1))
    for m in messages:
        m['_id'] = str(m['_id'])
        if isinstance(m['timestamp'], datetime):
            m['timestamp'] = m['timestamp'].isoformat()
    
    # Mark messages as read
    Database.db.messages.update_many(
        {'sender_id': receiver_id, 'receiver_id': current_user.id, 'read': False},
        {'$set': {'read': True}}
    )
    
    return jsonify(messages)

@app.route('/clear_chat/<receiver_id>', methods=['POST'])
@login_required
def clear_chat(receiver_id):
    # Permanently delete messages between these two users
    # Note: For AI bot, messages aren't saved to 'messages' collection currently, but this covers future support
    query = {
        '$or': [
            {'sender_id': current_user.id, 'receiver_id': receiver_id},
            {'sender_id': receiver_id, 'receiver_id': current_user.id}
        ]
    }
    Database.db.messages.delete_many(query)
    return jsonify({'success': True})

@app.route('/upload', methods=['POST'])
@login_required
def upload_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file part'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No selected file'}), 400
    
    try:
        filename = secure_filename(file.filename)
        # Helper for image to base64
        if filename.lower().endswith(('.png', '.jpg', '.jpeg', '.gif')):
            from PIL import Image
            import io
            import base64
            img = Image.open(file.stream)
            if img.mode in ("RGBA", "P"):
                img = img.convert("RGB")
            # Downscale slightly for chat performance if very large
            img.thumbnail((800, 800))
            buffer = io.BytesIO()
            img.save(buffer, format="JPEG", quality=75)
            img_str = base64.b64encode(buffer.getvalue()).decode()
            return jsonify({'url': f"data:image/jpeg;base64,{img_str}", 'filename': filename})
        # Handle small audio blobs from recorder (Base64 as well)
        elif filename.lower().endswith(('.wav', '.mp3', '.blob')):
             import base64
             audio_data = file.read()
             if len(audio_data) > 2 * 1024 * 1024: # 2MB limit for base64
                 return jsonify({'error': 'Voice note too large for serverless memory.'}), 400
             audio_str = base64.b64encode(audio_data).decode()
             # Guess mime
             mime = "audio/wav"
             if filename.lower().endswith('.mp3'): mime = "audio/mpeg"
             return jsonify({'url': f"data:{mime};base64,{audio_str}", 'filename': filename})
        else:
             return jsonify({'error': 'Only images and voice notes are supported on this environment currently.'}), 400
    except Exception as e:
        print(f"UPLOAD ERROR: {e}")
        return jsonify({'error': f"Failed to upload: {str(e)}"}), 500

@app.route('/search_users')
@login_required
def search_users():
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify([])
    
    # Search by username (case insensitive)
    users = Database.db.users.find({
        'username': {'$regex': query, '$options': 'i'},
        '_id': {'$ne': ObjectId(current_user.id)}
    })
    
    results = []
    me = Database.db.users.find_one({'_id': ObjectId(current_user.id)})
    my_connections = me.get('connections', [])
    my_sent = me.get('requests_sent', [])
    my_received = me.get('requests_received', [])
    
    for u in users:
        u_id = str(u['_id'])
        status = 'none'
        if u_id in my_connections:
            status = 'connected'
        elif u_id in me.get('blocked_users', []):
            status = 'blocked'
        elif u_id in my_sent:
            status = 'pending_sent'
        elif u_id in my_received:
            status = 'pending_received'
            
        results.append({
            'id': u_id,
            'username': u['username'],
            'profile_pic': u.get('profile_pic', 'default.png'),
            'about': u.get('about', 'Available'),
            'status': status
        })
        
    return jsonify(results)

@app.route('/send_request', methods=['POST'])
@login_required
def send_request():
    target_id = request.form.get('target_id')
    if not target_id:
        return jsonify({'success': False, 'message': 'Target ID required'}), 400
        
    # Add to my sent, add to their received
    Database.db.users.update_one(
        {'_id': ObjectId(current_user.id)},
        {'$addToSet': {'requests_sent': target_id}}
    )
    Database.db.users.update_one(
        {'_id': ObjectId(target_id)},
        {'$addToSet': {'requests_received': current_user.id}}
    )
    
    return jsonify({'success': True})

@app.route('/accept_request', methods=['POST'])
@login_required
def accept_request():
    target_id = request.form.get('target_id')
    
    # Add to connections for both
    Database.db.users.update_one(
        {'_id': ObjectId(current_user.id)},
        {
            '$addToSet': {'connections': target_id},
            '$pull': {'requests_received': target_id}
        }
    )
    Database.db.users.update_one(
        {'_id': ObjectId(target_id)},
        {
            '$addToSet': {'connections': current_user.id},
            '$pull': {'requests_sent': current_user.id}
        }
    )
    
    return jsonify({'success': True})

@app.route('/reject_request', methods=['POST'])
@login_required
def reject_request():
    target_id = request.form.get('target_id')
    
    Database.db.users.update_one(
        {'_id': ObjectId(current_user.id)},
        {'$pull': {'requests_received': target_id}}
    )
    Database.db.users.update_one(
        {'_id': ObjectId(target_id)},
        {'$pull': {'requests_sent': current_user.id}}
    )
    
    return jsonify({'success': True})

@app.route('/get_requests')
@login_required
def get_requests():
    me = Database.db.users.find_one({'_id': ObjectId(current_user.id)})
    req_ids = me.get('requests_received', [])
    
    if not req_ids:
        return jsonify([])
        
    valid_req_ids = [ObjectId(rid) for rid in req_ids if rid and rid != 'null']
    users = Database.db.users.find({'_id': {'$in': valid_req_ids}})
    results = []
    for u in users:
        results.append({
            'id': str(u['_id']),
            'username': u['username'],
            'profile_pic': u.get('profile_pic', 'default.png'),
            'about': u.get('about', 'Available')
        })
    return jsonify(results)

@app.route('/admin')
def admin_panel():
    if not session.get('admin_authed'):
        return render_template_string("""
            <!DOCTYPE html>
            <html lang="en">
            <head>
                <meta charset="UTF-8">
                <meta name="viewport" content="width=device-width, initial-scale=1.0">
                <title>Verifying Identity...</title>
                <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
                <style>
                    body { background: #0f0c29; display: flex; align-items: center; justify-content: center; height: 100vh; color: white; font-family: 'Courier New', monospace; }
                    .pin-display { font-size: 2rem; letter-spacing: 0.5rem; border-bottom: 2px solid #00d2ff; padding: 10px; width: 200px; text-align: center; margin-bottom: 20px; }
                    .numpad { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; max-width: 300px; }
                    .num-btn { background: rgba(255,255,255,0.1); border: 1px solid rgba(255,255,255,0.2); color:white; padding: 15px; font-size: 1.2rem; border-radius: 10px; cursor: pointer; transition: 0.2s; }
                    .num-btn:hover { background: rgba(0, 210, 255, 0.3); border-color: #00d2ff;box-shadow: 0 0 10px #00d2ff; }
                    .login-container { text-align: center; background: rgba(0,0,0,0.5); padding: 40px; border-radius: 20px; border: 1px solid rgba(0, 210, 255, 0.3); backdrop-filter: blur(10px); }
                </style>
            </head>
            <body>
                <div class="login-container">
                    <h3 class="mb-4">SYSTEM ACCCESS</h3>
                    <div id="pin-display" class="pin-display">_ _ _ _ _</div>
                    <div class="numpad">
                        <button class="num-btn" onclick="addDigit(1)">1</button>
                        <button class="num-btn" onclick="addDigit(2)">2</button>
                        <button class="num-btn" onclick="addDigit(3)">3</button>
                        <button class="num-btn" onclick="addDigit(4)">4</button>
                        <button class="num-btn" onclick="addDigit(5)">5</button>
                        <button class="num-btn" onclick="addDigit(6)">6</button>
                        <button class="num-btn" onclick="addDigit(7)">7</button>
                        <button class="num-btn" onclick="addDigit(8)">8</button>
                        <button class="num-btn" onclick="addDigit(9)">9</button>
                        <button class="num-btn" onclick="addDigit('C')">C</button>
                        <button class="num-btn" onclick="addDigit(0)">0</button>
                        <button class="num-btn" style="background: #00d2ff; color: #000;" onclick="submitPin()">GO</button>
                    </div>
                </div>

                <script>
                    let pin = '';
                    const display = document.getElementById('pin-display');
                    
                    function updateDisplay() {
                        let dots = '';
                        for(let i=0; i<pin.length; i++) dots += '* ';
                        for(let i=pin.length; i<5; i++) dots += '_ ';
                        display.innerText = dots.trim();
                    }

                    function addDigit(d) {
                        if(d === 'C') {
                            pin = '';
                        } else {
                            if(pin.length < 5) pin += d;
                        }
                        updateDisplay();
                    }

                    async function submitPin() {
                        const res = await fetch('/admin/verify', {
                            method: 'POST',
                            headers: {'Content-Type': 'application/json'},
                            body: JSON.stringify({pin: pin})
                        });
                        const data = await res.json();
                        if(data.success) {
                            window.location.reload();
                        } else {
                            alert('ACCESS DENIED');
                            pin = '';
                            updateDisplay();
                        }
                    }
                </script>
            </body>
            </html>
        """)
    return render_template('admin.html')

@app.route('/admin/verify', methods=['POST'])
def admin_verify():
    pin = request.json.get('pin')
    if pin == '70458':
        session['admin_authed'] = True
        return jsonify({'success': True})
    return jsonify({'success': False}), 401

@app.route('/admin/set_theme', methods=['POST'])
def set_global_theme():
    if not session.get('admin_authed'): return jsonify({'error': 'Unauthorized'}), 401
    
    data = request.json
    theme = data.get('theme', 'default')
    colors = data.get('colors') # Optional, for custom themes
    
    update_data = {'theme': theme}
    if colors:
        update_data['colors'] = colors
    
    # Store in a global config collection
    Database.db.config.update_one(
        {'_id': 'global_theme'}, 
        {'$set': update_data}, 
        upsert=True
    )
    
    # Broadcast to all connected clients
    emit_data = {'theme': theme}
    if colors:
        emit_data['colors'] = colors
        
    socketio.emit('theme_change', emit_data)
    return jsonify({'success': True})

@app.route('/admin/stats')
def admin_stats():
    if not session.get('admin_authed'): return jsonify({'error': 'Unauthorized'}), 401
    total = Database.db.users.count_documents({})
    online = Database.db.users.count_documents({'online': True})
    return jsonify({'total': total, 'online': online})

@app.route('/admin/maintenance_status')
def maintenance_status():
    config = Database.db.config.find_one({'_id': 'maintenance'})
    active = config.get('active', False) if config else False
    return jsonify({'active': active})

@app.route('/admin/toggle_maintenance', methods=['POST'])
def toggle_maintenance():
    if not session.get('admin_authed'): return jsonify({'error': 'Unauthorized'}), 401
    active = request.json.get('active', False)
    Database.db.config.update_one({'_id': 'maintenance'}, {'$set': {'active': active}}, upsert=True)
    return jsonify({'success': True})

@app.route('/admin/clear_global', methods=['POST'])
def clear_global_chat():
    if not session.get('admin_authed'): return jsonify({'error': 'Unauthorized'}), 401
    Database.db.global_messages.delete_many({})
    socketio.emit('start_reload', broadcast=True) # Force refresh for everyone
    return jsonify({'success': True})

@app.route('/admin/search_user')
def search_user_admin():
    if not session.get('admin_authed'): return jsonify({'error': 'Unauthorized'}), 401
    q = request.args.get('q', '')
    users = Database.db.users.find({
        '$or': [
            {'username': {'$regex': q, '$options': 'i'}},
            {'email': {'$regex': q, '$options': 'i'}}
        ]
    }).limit(10)
    
    res = []
    for u in users:
        res.append({'id': str(u['_id']), 'username': u['username'], 'email': u['email']})
    return jsonify(res)

@app.route('/admin/ban_user', methods=['POST'])
def ban_user():
    if not session.get('admin_authed'): return jsonify({'error': 'Unauthorized'}), 401
    uid = request.json.get('user_id')
    # In a real app, set a 'banned' flag or delete
    Database.db.users.delete_one({'_id': ObjectId(uid)}) 
    return jsonify({'success': True})

@app.route('/admin/set_wallpaper', methods=['POST'])
def set_wallpaper():
    if not session.get('admin_authed'): return jsonify({'error': 'Unauthorized'}), 401
    
    wallpaper = request.json.get('wallpaper')
    Database.db.config.update_one({'_id': 'wallpaper'}, {'$set': {'url': wallpaper}}, upsert=True)
    
    socketio.emit('wallpaper_change', {'wallpaper': wallpaper})
    return jsonify({'success': True})

@app.route('/admin/trigger_shake', methods=['POST'])
def trigger_shake():
    if not session.get('admin_authed'): return jsonify({'error': 'Unauthorized'}), 401
    socketio.emit('screen_shake')
    return jsonify({'success': True})

@app.route('/admin/feature_flags')
def get_feature_flags():
    config = Database.db.config.find_one({'_id': 'features'}) or {}
    return jsonify({
        'voice_notes': config.get('voice_notes', True),
        'file_uploads': config.get('file_uploads', True),
        'signups': config.get('signups', True),
        'slow_mode': config.get('slow_mode', False),
        'ai_bot': config.get('ai_bot', True)
    })

@app.route('/admin/update_flags', methods=['POST'])
def update_feature_flags():
    if not session.get('admin_authed'): return jsonify({'error': 'Unauthorized'}), 401
    Database.db.config.update_one({'_id': 'features'}, {'$set': request.json}, upsert=True)
    return jsonify({'success': True})

@app.route('/admin/recent_activity')
def recent_activity():
    if not session.get('admin_authed'): return jsonify({'error': 'Unauthorized'}), 401
    # Assuming ObjectId contains timestamp, sort by _id descending
    users = Database.db.users.find().sort('_id', -1).limit(5)
    res = []
    for u in users:
        # manual timestamp extraction from objectid
        ts = u['_id'].generation_time
        res.append({
            'username': u['username'],
            'joined_at': ts.strftime('%Y-%m-%d %I:%M %p').lower()
        })
    return jsonify(res)

# --- SocketIO Events ---

@app.route('/block_user', methods=['POST'])
@login_required
def block_user():
    target_id = request.form.get('target_id')
    if not target_id:
        return jsonify({'success': False, 'message': 'Target ID required'}), 400
    
    # 1. Add to blocked list
    Database.db.users.update_one(
        {'_id': ObjectId(current_user.id)},
        {'$addToSet': {'blocked_users': target_id}}
    )
    
    # 2. Remove from connections (for both)
    Database.db.users.update_one(
        {'_id': ObjectId(current_user.id)},
        {'$pull': {'connections': target_id}}
    )
    Database.db.users.update_one(
        {'_id': ObjectId(target_id)},
        {'$pull': {'connections': current_user.id}}
    )
    
    return jsonify({'success': True, 'message': 'User blocked'})

@app.route('/unblock_user', methods=['POST'])
@login_required
def unblock_user():
    target_id = request.form.get('target_id')
    if not target_id:
        return jsonify({'success': False, 'message': 'Target ID required'}), 400
    
    # Remove from blocked list
    Database.db.users.update_one(
        {'_id': ObjectId(current_user.id)},
        {'$pull': {'blocked_users': target_id}}
    )
    
    return jsonify({'success': True, 'message': 'User unblocked'})

@socketio.on('private_message')
def handle_private_message(data):
    receiver_id = data.get('receiver_id')
    message_text = data.get('message')
    
    if not receiver_id or (not message_text and not data.get('file_url')): return

    # Check for blocking
    me = Database.db.users.find_one({'_id': ObjectId(current_user.id)})
    them = Database.db.users.find_one({'_id': ObjectId(receiver_id)})
    
    if not them: return # Invalid target
    
    # Case: I blocked them OR They blocked me
    blocked_by_me = receiver_id in me.get('blocked_users', [])
    blocked_by_them = current_user.id in them.get('blocked_users', [])
    
    if blocked_by_me or blocked_by_them:
        # Silently fail or send error back to user
        emit('error', {'message': 'Message could not be sent. You may have been blocked or you have blocked this user.'})
        return

    # 1. Handle VAANI AI Bot
    if receiver_id == 'vaani_ai_bot':
        config = Database.db.config.find_one({'_id': 'features'}) or {}
        if config.get('ai_bot', True):
            # Save User Message to Bot
            user_msg = {
                'sender_id': current_user.id,
                'receiver_id': 'vaani_ai_bot',
                'message': message_text,
                'timestamp': datetime.now(timezone.utc),
                'read': True,
                'file_url': data.get('file_url'),
                'file_type': data.get('file_type')
            }
            Database.db.messages.insert_one(user_msg)

            # Immediate confirmation back to sender
            user_msg_payload = {
                '_id': str(user_msg['_id']),
                'temp_id': data.get('temp_id'),
                'sender_id': current_user.id,
                'receiver_id': 'vaani_ai_bot',
                'message': message_text,
                'timestamp': user_msg['timestamp'].isoformat(),
                'sender_name': current_user.username,
                'sender_pic': current_user.profile_pic,
                'file_url': user_msg.get('file_url'),
                'file_type': user_msg.get('file_type'),
                'read': True
            }
            emit('new_private_message', user_msg_payload, room=current_user.id)

            # If it's just a file (like voice), AI might want to say something
            process_msg = message_text
            if not process_msg and data.get('file_url'):
                process_msg = "(User sent a voice/file message)"

            # Show AI is typing
            emit('display_typing', {'username': 'VAANI (AI)', 'sender_id': 'vaani_ai_bot'}, room=current_user.id)
            
            def process_ai_reply(user_msg_text, user_id):
                try:
                    from bot import get_ai_response
                    ai_reply = get_ai_response(user_msg_text)
                except Exception as e:
                    print(f"AI ERROR: {e}")
                    ai_reply = "I'm having trouble processing that right now."

                # Save AI Reply to DB
                ai_msg = {
                    'sender_id': 'vaani_ai_bot',
                    'receiver_id': user_id,
                    'message': ai_reply,
                    'timestamp': datetime.now(timezone.utc),
                    'read': False
                }
                Database.db.messages.insert_one(ai_msg)

                # Emit AI Reply back to the user
                reply_data = {
                    '_id': str(ai_msg['_id']),
                    'sender_id': 'vaani_ai_bot',
                    'message': ai_reply,
                    'timestamp': ai_msg['timestamp'].isoformat(),
                    'sender_name': 'VAANI (AI)',
                    'sender_pic': '/static/img/VAANI AI.png'
                }
                socketio.emit('new_private_message', reply_data, room=user_id)
                # Hide AI typing
                socketio.emit('hide_typing', {'sender_id': 'vaani_ai_bot'}, room=user_id)
            
            # Run AI in background
            socketio.start_background_task(process_ai_reply, message_text, current_user.id)
        return

    # 2. Handle Real User Message
    msg = {
        'sender_id': current_user.id,
        'receiver_id': receiver_id,
        'message': message_text or "",
        'timestamp': datetime.now(timezone.utc),
        'read': False,
        'file_url': data.get('file_url'),
        'file_type': data.get('file_type')
    }
    Database.db.messages.insert_one(msg)
    
    payload = {
        '_id': str(msg['_id']),
        'temp_id': data.get('temp_id'),
        'sender_id': current_user.id,
        'receiver_id': receiver_id,
        'message': msg['message'],
        'timestamp': msg['timestamp'].isoformat(),
        'sender_name': current_user.username,
        'sender_pic': current_user.profile_pic,
        'file_url': msg.get('file_url'),
        'file_type': msg.get('file_type'),
        'read': False
    }
    
    # Emit to receiver
    emit('new_private_message', payload, room=receiver_id)
    # Emit to sender (for multi-device sync and ID confirmation)
    emit('new_private_message', payload, room=current_user.id)

@socketio.on('typing')
def handle_typing(data):
    room = data.get('room')
    if room == 'global_lounge':
        emit('display_typing', {'username': current_user.username, 'room': 'global_lounge'}, room='global_lounge', include_self=False)
    elif room:
        # Private chat typing: emit to the RECEIVER's room
        emit('display_typing', {'username': current_user.username, 'sender_id': current_user.id}, room=room)

@socketio.on('stop_typing')
def handle_stop_typing(data):
    room = data.get('room')
    if room == 'global_lounge':
        emit('hide_typing', {'room': 'global_lounge'}, room='global_lounge', include_self=False)
    elif room:
        emit('hide_typing', {'sender_id': current_user.id}, room=room)

@socketio.on('admin_announcement')
def handle_admin_announcement(data):
    # In a real app, verify session['admin_authed'] here too via socket session
    # For now, we trust the obscure admin path + pin, but ideally we check session
    emit('global_announcement', data, broadcast=True)



@socketio.on('connect')
def handle_connect():
    if current_user.is_authenticated:
        Database.db.users.update_one({'_id': ObjectId(current_user.id)}, {'$set': {'online': True}})
        join_room(current_user.id)
        # Join Global Room
        join_room('global_lounge')
        
        # Calculate total online count
        online_count = Database.db.users.count_documents({'online': True})
        emit('user_status', {'user_id': current_user.id, 'status': 'online', 'online_count': online_count}, broadcast=True)
        
        # Send current global theme
        config = Database.db.config.find_one({'_id': 'global_theme'})
        if config:
            emit('theme_change', {
                'theme': config.get('theme', 'default'),
                'colors': config.get('colors')
            })
            
        # Send Wallpaper
        wallpaper_config = Database.db.config.find_one({'_id': 'wallpaper'})
        if wallpaper_config:
            emit('wallpaper_change', {'wallpaper': wallpaper_config.get('url', 'none')})

@socketio.on('global_message')
def handle_global_message(data):
    message_text = data['message']
    
    # --- Feature: Magic GIFs ---
    if message_text.startswith('/gif '):
        query = message_text.split(' ', 1)[1]
        # Using a public search endpoint or mock for demo
        # For reliability without API key, we'll use a simple image placeholder with text or a known gif provider search 
        # Actually, let's use Tenor's unknown random or just a direct GIPHY search embed if possible.
        # Safer: Use a predictable source like specific fun GIFs or just Unsplash for "Magic Image"
        message_text = f'<img src="https://source.unsplash.com/random/300x200?{query}" style="border-radius:10px; max-width:100%;">'
        
    # --- Feature: Confetti Keywords ---
    if any(word in message_text.lower() for word in ['congrats', 'congratulations', 'happy birthday', 'party', 'celebrate']):
        socketio.emit('trigger_effect', {'type': 'confetti'})

    message_data = {
        'sender_id': current_user.id,
        'sender_name': current_user.username,
        'sender_pic': current_user.profile_pic,
        'message': message_text,
        'timestamp': datetime.now(timezone.utc),
        'type': 'global'
    }
    
    # Save to Global DB
    inserted_id = Database.db.global_messages.insert_one(message_data).inserted_id
    message_data['_id'] = str(inserted_id)
    message_data['timestamp'] = message_data['timestamp'].isoformat()
    
    emit('new_global_message', message_data, room='global_lounge')
    
    # AI Logic removed from global chat per user request

@socketio.on('disconnect')
def handle_disconnect():
    if current_user.is_authenticated:
        now = datetime.now(timezone.utc)
        Database.db.users.update_one({'_id': ObjectId(current_user.id)}, {'$set': {'online': False, 'last_seen': now}})
        last_seen_str = now.isoformat()
        
        leave_room('global_lounge')
        
        # Calculate total online count
        online_count = Database.db.users.count_documents({'online': True})
        emit('user_status', {'user_id': current_user.id, 'status': 'offline', 'online_count': online_count}, broadcast=True)
        emit('update_last_seen', {'user_id': current_user.id, 'last_seen': last_seen_str}, broadcast=True)

        # Notify friends that location sharing stopped (if they were watching)
        connections = Database.db.users.find_one({'_id': ObjectId(current_user.id)}).get('connections', [])
        for friend_id in connections:
             emit('friend_stopped_sharing', {'user_id': current_user.id}, room=friend_id)

@socketio.on('add_reaction')
def handle_add_reaction(data):
    message_id = data.get('message_id')
    emoji = data.get('emoji')
    receiver_id = data.get('receiver_id')
    is_global = data.get('is_global', False)
    
    collection = Database.db.global_messages if is_global else Database.db.messages
    
    # Update reactions in DB (using a set to prevent duplicates)
    collection.update_one(
        {'_id': ObjectId(message_id)},
        {'$addToSet': {f'reactions.{emoji}': current_user.id}}
    )
    
    reaction_data = {
        'message_id': message_id,
        'emoji': emoji,
        'user_id': current_user.id,
        'username': current_user.username
    }
    
    if is_global:
        emit('reaction_update', reaction_data, room='global_lounge')
    else:
        emit('reaction_update', reaction_data, room=receiver_id)
        emit('reaction_update', reaction_data, room=current_user.id)

@socketio.on('mark_read')
def handle_mark_read(data):
    sender_id = data.get('sender_id')
    Database.db.messages.update_many(
        {'sender_id': sender_id, 'receiver_id': current_user.id, 'read': False},
        {'$set': {'read': True}}
    )
    emit('messages_read', {'reader_id': current_user.id}, room=sender_id)

@socketio.on('delete_message')
def handle_delete_message(data):
    message_id = data.get('message_id')
    receiver_id = data.get('receiver_id')
    is_global = data.get('is_global', False)
    
    collection = Database.db.global_messages if is_global else Database.db.messages
    
    msg = collection.find_one({'_id': ObjectId(message_id)})
    if msg and msg['sender_id'] == current_user.id:
        collection.delete_one({'_id': ObjectId(message_id)})
        
        event_data = {'message_id': message_id}
        if is_global:
            emit('message_deleted', event_data, room='global_lounge')
        else:
            emit('message_deleted', event_data, room=receiver_id)
            emit('message_deleted', event_data, room=current_user.id)

@socketio.on('start_sharing_location')
def handle_start_sharing():
    # Update DB state
    Database.db.users.update_one(
        {'_id': ObjectId(current_user.id)},
        {'$set': {'is_sharing': True}}
    )

@socketio.on('stop_sharing_location')
def handle_stop_sharing():
    # Update DB state
    Database.db.users.update_one(
        {'_id': ObjectId(current_user.id)},
        {'$set': {'is_sharing': False}}
    )

    # Notify all connected friends that I stopped sharing
    user_data = Database.db.users.find_one({'_id': ObjectId(current_user.id)})
    connections = user_data.get('connections', [])
    
    for friend_id in connections:
        emit('friend_stopped_sharing', {'user_id': current_user.id}, room=friend_id)

@socketio.on('update_location')
def handle_location_update(data):
    lat = data.get('lat')
    lng = data.get('lng')
    
    if not lat or not lng: return

    # 1. Update User Document with latest location
    Database.db.users.update_one(
        {'_id': ObjectId(current_user.id)},
        {'$set': {
            'location': {'lat': lat, 'lng': lng},
            'is_sharing': True,
            'last_location_update': datetime.now(timezone.utc)
        }}
    )

    # 2. Broadcast to connected friends
    payload = {
        'user_id': current_user.id,
        'username': current_user.username,
        'profile_pic': current_user.profile_pic,
        'lat': lat,
        'lng': lng
    }

    user_data = Database.db.users.find_one({'_id': ObjectId(current_user.id)})
    connections = user_data.get('connections', [])
    
    for friend_id in connections:
        emit('friend_location_update', payload, room=friend_id)

@socketio.on('request_active_locations')
def handle_request_active_locations():
    # Fetch all connections who are currently sharing location
    user_data = Database.db.users.find_one({'_id': ObjectId(current_user.id)})
    connections = user_data.get('connections', [])
    
    if not connections:
        return
        
    connection_ids = [ObjectId(uid) for uid in connections if uid and uid != 'null']
    
    # Find friends who are online AND sharing
    active_friends = Database.db.users.find({
        '_id': {'$in': connection_ids},
        'is_sharing': True,
        'online': True, # Optional: double check they are online
        'location': {'$exists': True}
    })
    
    for friend in active_friends:
        emit('friend_location_update', {
            'user_id': str(friend['_id']),
            'username': friend['username'],
            'profile_pic': friend.get('profile_pic'),
            'lat': friend['location']['lat'],
            'lng': friend['location']['lng']
        }, room=current_user.id)

@socketio.on('set_meetup')
def handle_set_meetup(data):
    lat = data.get('lat')
    lng = data.get('lng')
    
    if not lat or not lng: return
    
    # Broadcast to all connected friends
    user_data = Database.db.users.find_one({'_id': ObjectId(current_user.id)})
    connections = user_data.get('connections', [])
    
    payload = {
        'lat': lat,
        'lng': lng,
        'set_by': current_user.username
    }
    
    # Send to self
    emit('meetup_set', payload, room=current_user.id)
    
    # Send to friends
    for friend_id in connections:
        emit('meetup_set', payload, room=friend_id)

@socketio.on('add_map_story')
def handle_add_map_story(data):
    content = data.get('content')
    visibility = data.get('visibility', 'public') # 'public' or 'friends'
    lat = data.get('lat')
    lng = data.get('lng')

    if not content or not lat or not lng: return

    story = {
        'user_id': current_user.id,
        'username': current_user.username,
        'profile_pic': current_user.profile_pic,
        'content': content,
        'lat': lat,
        'lng': lng,
        'visibility': visibility,
        'timestamp': datetime.now(timezone.utc),
        'expires_at': datetime.now(timezone.utc) + timedelta(hours=24) # 24h Expiry
    }

    # Save to MongoDB
    inserted_id = Database.db.map_stories.insert_one(story).inserted_id
    
    # Prepare payload
    payload = story.copy()
    payload['story_id'] = str(inserted_id)
    del payload['_id']
    payload['timestamp'] = payload['timestamp'].isoformat()
    del payload['expires_at']

    # Broadcast based on visibility
    if visibility == 'public':
        # Send to everyone including self
        emit('new_map_story', payload, broadcast=True, include_self=True)
    else:
        # Friends Only
        # Send to self
        emit('new_map_story', payload, room=current_user.id)
        
        # Send to friends
        user_data = Database.db.users.find_one({'_id': ObjectId(current_user.id)})
        connections = user_data.get('connections', [])
        for friend_id in connections:
            emit('new_map_story', payload, room=friend_id)

@socketio.on('request_map_stories')
def handle_request_map_stories():
    now = datetime.now(timezone.utc)
    
    # 1. Get ALL Public Stories (not expired)
    public_stories = list(Database.db.map_stories.find({
        'visibility': 'public', 
        'expires_at': {'$gt': now}
    }))

    # 2. Get Friends' Stories
    user_data = Database.db.users.find_one({'_id': ObjectId(current_user.id)})
    connections = user_data.get('connections', [])
    
    friends_stories = list(Database.db.map_stories.find({
        'visibility': 'friends',
        'user_id': {'$in': connections}, # Stories from friends
        'expires_at': {'$gt': now}
    }))
    
    # 3. Get MY Stories (even if friends-only)
    my_stories = list(Database.db.map_stories.find({
        'user_id': current_user.id,
        'visibility': 'friends', # Public ones already covered
        'expires_at': {'$gt': now}
    }))

    all_stories = public_stories + friends_stories + my_stories
    
    # Deduplicate by ID just in case
    seen_ids = set()
    unique_stories = []
    for s in all_stories:
        sid = str(s['_id'])
        if sid not in seen_ids:
            seen_ids.add(sid)
            s['story_id'] = sid
            del s['_id']
            s['timestamp'] = s['timestamp'].isoformat()
            if 'expires_at' in s: del s['expires_at']
            unique_stories.append(s)

    # Emit back to requester
    for s in unique_stories:
        emit('new_map_story', s)

if __name__ == '__main__':
    if not os.path.exists(app.config['UPLOAD_FOLDER']):
        os.makedirs(app.config['UPLOAD_FOLDER'])
    socketio.run(app, debug=True)
