import os
import json
import time
import psycopg2
import psycopg2.extras
from flask import Flask, render_template_string, request, jsonify, session
from playwright.sync_api import sync_playwright
from functools import wraps
import secrets
import uuid

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', secrets.token_hex(32))

# ============================================================
# НАСТРОЙКИ
# ============================================================
DEEPSEEK_SESSION_ID = "eac394942672455abc38d9afbe989c1b"
DB_URL = "postgresql://bothost_db_06a851292493:ToKKxst8x1doT6bVcHgfjr7AV8czk0jGA86XlKI0zyo@node1.pghost.ru:15918/bothost_db_06a851292493"

def get_db_connection():
    return psycopg2.connect(DB_URL)

def init_db():
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id SERIAL PRIMARY KEY,
                    session_id VARCHAR(255) NOT NULL,
                    title VARCHAR(255) DEFAULT 'New Chat',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS messages (
                    id SERIAL PRIMARY KEY,
                    conversation_id INTEGER REFERENCES conversations(id),
                    role VARCHAR(50) NOT NULL,
                    content TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        conn.commit()

class DeepSeekBrowser:
    def __init__(self):
        self.session_id = DEEPSEEK_SESSION_ID
        self.headless = os.environ.get('HEADLESS', 'true').lower() == 'true'
        
        self.cookies = [
            {
                "name": "dc_session_id",
                "value": self.session_id,
                "domain": ".deepseek.com",
                "path": "/",
                "httpOnly": True,
                "secure": True,
                "sameSite": "Lax"
            }
        ]
    
    def send_message(self, message):
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=self.headless,
                args=['--no-sandbox', '--disable-dev-shm-usage']
            )
            
            context = browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            )
            
            context.add_cookies(self.cookies)
            page = context.new_page()
            
            try:
                page.goto('https://chat.deepseek.com/', wait_until='networkidle', timeout=30000)
                time.sleep(3)
                
                # Ищем поле ввода
                textarea = page.locator('textarea').first
                textarea.click()
                time.sleep(0.5)
                textarea.fill(message)
                time.sleep(0.5)
                
                # Нажимаем Enter
                page.keyboard.press('Enter')
                
                # Ждем появления ответа
                time.sleep(15)
                
                # Пробуем получить ответ разными способами
                response_text = None
                
                # Способ 1: ищем все блоки с текстом
                all_divs = page.locator('div').all()
                texts = []
                for div in all_divs[-10:]:  # Берем последние 10 блоков
                    try:
                        text = div.inner_text()
                        if text and len(text) > 20:
                            texts.append(text)
                    except:
                        pass
                
                if texts:
                    # Берем самый длинный текст (скорее всего ответ)
                    response_text = max(texts, key=len)
                
                # Способ 2: если не нашли, ищем по классам
                if not response_text:
                    selectors = [
                        '.ds-markdown',
                        '[class*="markdown"]',
                        '.prose',
                        '.whitespace-pre-wrap',
                        '[class*="message"]'
                    ]
                    for selector in selectors:
                        try:
                            elements = page.locator(selector).all()
                            for elem in elements:
                                text = elem.inner_text()
                                if text and len(text) > 20:
                                    response_text = text
                                    break
                            if response_text:
                                break
                        except:
                            continue
                
                return response_text if response_text else "Не удалось получить ответ"
                
            except Exception as e:
                return f"Ошибка: {str(e)}"
            finally:
                browser.close()

deepseek = DeepSeekBrowser()

def get_session():
    if 'session_id' not in session:
        session['session_id'] = str(uuid.uuid4())
    return session['session_id']

CHAT_HTML = '''
<!DOCTYPE html>
<html>
<head><title>DeepSeek Chat</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:Arial;height:100vh;display:flex}
.sidebar{width:280px;background:#1a1a2e;color:#fff;display:flex;flex-direction:column}
.sidebar-header{padding:20px;border-bottom:1px solid #333}
.new-chat-btn{width:100%;padding:12px;background:#667eea;color:#fff;border:none;border-radius:8px;cursor:pointer}
.conversations-list{flex:1;overflow-y:auto;padding:10px}
.conversation-item{padding:12px;margin-bottom:5px;border-radius:8px;cursor:pointer;font-size:14px}
.conversation-item:hover,.conversation-item.active{background:#2a2a4e}
.main-chat{flex:1;display:flex;flex-direction:column;background:#f5f5f5}
.chat-header{padding:20px;background:#fff;border-bottom:1px solid #e0e0e0}
.messages-container{flex:1;overflow-y:auto;padding:20px}
.message{margin-bottom:20px;display:flex}
.message.user{justify-content:flex-end}
.message-content{max-width:70%;padding:12px 16px;border-radius:12px;font-size:14px;line-height:1.5}
.user .message-content{background:#667eea;color:#fff}
.assistant .message-content{background:#fff;color:#333}
.input-container{padding:20px;background:#fff;border-top:1px solid #e0e0e0}
.input-wrapper{display:flex;gap:10px}
#messageInput{flex:1;padding:12px;border:1px solid #ddd;border-radius:8px;font-size:14px}
#sendButton{padding:12px 24px;background:#667eea;color:#fff;border:none;border-radius:8px;cursor:pointer}
</style></head>
<body>
<div class="sidebar">
<div class="sidebar-header"><button class="new-chat-btn" onclick="newChat()">+ Новый чат</button></div>
<div class="conversations-list" id="conversationsList"></div>
</div>
<div class="main-chat">
<div class="chat-header"><h3>DeepSeek Chat</h3></div>
<div class="messages-container" id="messagesContainer">
<div style="text-align:center;color:#999;margin-top:50px">Начните новый чат</div>
</div>
<div class="input-container">
<div class="input-wrapper">
<input type="text" id="messageInput" placeholder="Введите сообщение..." onkeypress="if(event.key==='Enter')sendMessage()">
<button id="sendButton" onclick="sendMessage()">Отправить</button>
</div>
</div>
</div>
<script>
let currentConversationId = null;

async function loadConversations() {
    const response = await fetch('/api/conversations');
    const conversations = await response.json();
    const list = document.getElementById('conversationsList');
    list.innerHTML = conversations.map(c => 
        `<div class="conversation-item" onclick="loadConversation(${c.id})">${c.title}</div>`
    ).join('');
}

async function newChat() {
    currentConversationId = null;
    document.getElementById('messagesContainer').innerHTML = 
        '<div style="text-align:center;color:#999;margin-top:50px">Новый чат</div>';
    document.getElementById('messageInput').focus();
}

async function loadConversation(id) {
    currentConversationId = id;
    const response = await fetch(`/api/conversations/${id}/messages`);
    const messages = await response.json();
    const container = document.getElementById('messagesContainer');
    container.innerHTML = messages.map(m => 
        `<div class="message ${m.role}"><div class="message-content">${m.content}</div></div>`
    ).join('');
    container.scrollTop = container.scrollHeight;
}

async function sendMessage() {
    const input = document.getElementById('messageInput');
    const message = input.value.trim();
    if(!message) return;
    
    input.value = '';
    input.disabled = true;
    document.getElementById('sendButton').disabled = true;
    
    const container = document.getElementById('messagesContainer');
    container.innerHTML += `<div class="message user"><div class="message-content">${message}</div></div>`;
    container.innerHTML += '<div class="message assistant"><div class="message-content">Думаю...</div></div>';
    container.scrollTop = container.scrollHeight;
    
    try {
        const response = await fetch('/api/chat', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({message, conversation_id: currentConversationId})
        });
        
        const data = await response.json();
        currentConversationId = data.conversation_id;
        
        const messages = container.getElementsByClassName('message');
        const lastMessage = messages[messages.length - 1];
        lastMessage.querySelector('.message-content').textContent = data.assistant_message;
        
        loadConversations();
    } catch(e) {
        const messages = container.getElementsByClassName('message');
        const lastMessage = messages[messages.length - 1];
        lastMessage.querySelector('.message-content').textContent = 'Ошибка отправки';
    }
    
    input.disabled = false;
    document.getElementById('sendButton').disabled = false;
    input.focus();
}

loadConversations();
</script>
</body></html>
'''

@app.route('/')
def index():
    return render_template_string(CHAT_HTML)

@app.route('/api/conversations', methods=['GET'])
def get_conversations():
    session_id = get_session()
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT * FROM conversations WHERE session_id = %s ORDER BY updated_at DESC",
                (session_id,)
            )
            return jsonify([dict(c) for c in cur.fetchall()])

@app.route('/api/conversations/<int:conv_id>/messages', methods=['GET'])
def get_messages(conv_id):
    with get_db_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
            cur.execute(
                "SELECT * FROM messages WHERE conversation_id = %s ORDER BY created_at",
                (conv_id,)
            )
            return jsonify([dict(m) for m in cur.fetchall()])

@app.route('/api/chat', methods=['POST'])
def send_message():
    data = request.json
    message = data.get('message')
    conversation_id = data.get('conversation_id')
    session_id = get_session()
    
    if not message:
        return jsonify({'error': 'Message is required'}), 400
    
    if not conversation_id:
        with get_db_connection() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cur.execute(
                    "INSERT INTO conversations (session_id, title) VALUES (%s, %s) RETURNING *",
                    (session_id, message[:50] + '...')
                )
                conversation = cur.fetchone()
            conn.commit()
        conversation_id = conversation['id']
    
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO messages (conversation_id, role, content) VALUES (%s, %s, %s)",
                (conversation_id, 'user', message)
            )
        conn.commit()
    
    response = deepseek.send_message(message)
    
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO messages (conversation_id, role, content) VALUES (%s, %s, %s)",
                (conversation_id, 'assistant', response)
            )
            cur.execute(
                "UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                (conversation_id,)
            )
        conn.commit()
    
    return jsonify({
        'conversation_id': conversation_id,
        'assistant_message': response
    })

if __name__ == '__main__':
    init_db()
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
