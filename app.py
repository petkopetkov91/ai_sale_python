import os
import time
import requests
import xml.etree.ElementTree as ET
from flask import Flask, request, jsonify, render_template
from openai import OpenAI
from dotenv import load_dotenv
import json
import traceback
import re
import io
from supabase import create_client, Client

# Load environment variables
load_dotenv()

# Initialize Flask app and OpenAI client
app = Flask(__name__)
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
ASSISTANT_ID = os.getenv("OPENAI_ASSISTANT_ID")

# Supabase Client
supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_ANON_KEY")
supabase: Client = create_client(supabase_url, supabase_key)

# Vector Store ID
VECTOR_STORE_ID = os.getenv("VECTOR_STORE_ID")

def parse_price(price_str):
    """Extracts the price in BGN from a string like '35 858,96 € / 70 134,03 лв.'"""
    if not price_str:
        return float('inf')
    match = re.search(r'([\d\s,]+)\s*лв', price_str)
    if match:
        try:
            price_clean = match.group(1).replace(' ', '').replace(',', '.')
            return float(price_clean)
        except (ValueError, TypeError):
            return float('inf')
    return float('inf')

def get_available_cars(model_filter=None):
    """
    Fetches, filters, sorts by price, and returns the top 4 cheapest cars as a Python dictionary.
    """
    print(f"DEBUG: Calling get_available_cars function. Filter: {model_filter}")
    try:
        url = "https://sale.peugeot.bg/ecommerce/fb/product_feed.xml"
        response = requests.get(url, timeout=15)
        response.raise_for_status()

        root = ET.fromstring(response.content)
        all_cars = []
        ns = {'g': 'http://base.google.com/ns/1.0'}

        for item in root.findall('.//channel/item'):
            if item.find('g:availability', ns) is not None and item.find('g:availability', ns).text == 'in stock':
                all_cars.append({
                    "model": item.find('g:title', ns).text.strip() if item.find('g:title', ns) is not None else "N/A",
                    "price": item.find('g:description', ns).text.strip() if item.find('g:description', ns) is not None else "N/A",
                    "link": item.find('g:link', ns).text if item.find('g:link', ns) is not None else "#",
                    "image_url": item.find('g:image_link', ns).text if item.find('g:image_link', ns) is not None else ""
                })
        
        filtered_cars = [car for car in all_cars if model_filter.lower() in car['model'].lower()] if model_filter else all_cars
        
        for car in filtered_cars:
            car['numeric_price'] = parse_price(car['price'])

        sorted_cars = sorted(filtered_cars, key=lambda x: x['numeric_price'])
        final_cars = sorted_cars[:2]
        
        if not final_cars:
            summary = f"За съжаление, в момента няма налични автомобили, отговарящи на вашето търсене за '{model_filter}'." if model_filter else "За съжаление, в момента няма налични автомобили."
            return {"summary": summary, "cars": []}

        summary = "Ето налични автомобили, които отговарят на вашето търсене:"
        return {"summary": summary, "cars": final_cars}

    except Exception as e:
        traceback.print_exc()
        summary = "Възникна грешка при извличането на данните за автомобили."
        return {"summary": summary, "cars": []}

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/chat', methods=['POST'])
def chat():
    try:
        data = request.json
        thread_id = data.get("thread_id")
        user_message = data.get("message")
        is_new_thread = not thread_id

        if thread_id:
            try:
                session_res = supabase.table('chat_sessions').select('is_human_controlled').eq('session_id', thread_id).single().execute()
                if session_res.data and session_res.data.get('is_human_controlled'):
                    supabase.table('chat_messages').insert({"session_id": thread_id, "message": user_message, "is_user": True}).execute()
                    return jsonify({"response": "Този чат се обслужва от оператор.", "thread_id": thread_id, "is_new_thread": is_new_thread, "human_override": True})
            except Exception:
                pass

        if is_new_thread:
            thread = client.beta.threads.create()
            thread_id = thread.id
            supabase.table('chat_sessions').insert({"session_id": thread_id, "is_human_controlled": False}).execute()

        supabase.table('chat_messages').insert({"session_id": thread_id, "message": user_message, "is_user": True}).execute()
        client.beta.threads.messages.create(thread_id=thread_id, role="user", content=user_message)

        run = client.beta.threads.runs.create(
            assistant_id=ASSISTANT_ID,
            thread_id=thread_id,
            tool_resources={ "file_search": { "vector_store_ids": [VECTOR_STORE_ID] } }
        )
        while run.status in ['queued', 'in_progress', 'requires_action']:
            time.sleep(1)
            run = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
            if run.status == 'requires_action':
                tool_call = run.required_action.submit_tool_outputs.tool_calls[0]
                if tool_call.function.name == "get_available_cars":
                    arguments = json.loads(tool_call.function.arguments)
                    car_data = get_available_cars(model_filter=arguments.get('model_filter'))
                    supabase.table('chat_messages').insert({"session_id": thread_id, "message": car_data['summary'], "is_user": False}).execute()
                    client.beta.threads.runs.submit_tool_outputs(thread_id=thread_id, run_id=run.id, tool_outputs=[{"tool_call_id": tool_call.id, "output": "Function executed."}])
                    return jsonify({"response": car_data['summary'], "cars": car_data['cars'], "thread_id": thread_id, "is_new_thread": is_new_thread})

        if run.status == 'completed':
            messages = client.beta.threads.messages.list(thread_id=thread_id, order="desc", limit=1)
            response_text = messages.data[0].content[0].text.value
            supabase.table('chat_messages').insert({"session_id": thread_id, "message": response_text, "is_user": False}).execute()
            return jsonify({"response": response_text, "thread_id": thread_id, "is_new_thread": is_new_thread})
        else:
            return jsonify({"response": f"Грешка: {run.status}", "thread_id": thread_id, "is_new_thread": is_new_thread})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Сървърна грешка: {e}"}), 500

# --- API Routes for Chat History ---
@app.route('/api/threads', methods=['GET'])
def get_threads():
    try:
        response = supabase.table('chat_sessions').select('*').order('created_at', desc=True).execute()
        sessions = response.data
        threads_with_titles = []
        for session in sessions:
            msg_response = supabase.table('chat_messages').select('message').eq('session_id', session['session_id']).eq('is_user', True).order('created_at', desc=False).limit(1).execute()
            if msg_response.data:
                threads_with_titles.append({
                    "id": session['session_id'],
                    "title": msg_response.data[0]['message'],
                    "created_at": session['created_at'],
                    "is_human_controlled": session.get('is_human_controlled', False)
                })
        return jsonify(threads_with_titles)
    except Exception as e:
        return jsonify({"error": "Failed to retrieve threads."}), 500

@app.route('/api/threads/<string:thread_id>/messages', methods=['GET'])
def get_thread_messages(thread_id):
    try:
        response = supabase.table('chat_messages').select('message, is_user').eq('session_id', thread_id).order('created_at').execute()
        return jsonify([{"role": "user" if msg['is_user'] else "assistant", "content": msg['message']} for msg in response.data])
    except Exception as e:
        return jsonify({"error": "Failed to retrieve messages."}), 500

# --- Admin & Monitoring Routes ---
@app.route('/admin')
def admin():
    return render_template('admin.html')

@app.route('/monitoring')
def monitoring():
    return render_template('monitoring.html')

@app.route('/api/monitoring/send-message', methods=['POST'])
def handle_admin_message():
    data = request.json
    try:
        supabase.table('chat_sessions').update({'is_human_controlled': True}).eq('session_id', data["thread_id"]).execute()
        supabase.table('chat_messages').insert({"session_id": data["thread_id"], "message": data["message"], "is_user": False}).execute()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/vector-store/files', methods=['GET'])
def list_vector_store_files():
    if not VECTOR_STORE_ID: return jsonify({"error": "VECTOR_STORE_ID not configured."}), 500
    try:
        vector_store_files = client.beta.vector_stores.files.list(vector_store_id=VECTOR_STORE_ID)
        files_with_details = [client.files.retrieve(f.id) for f in vector_store_files.data]
        return jsonify([{"id": f.id, "filename": f.filename, "created_at": f.created_at} for f in files_with_details])
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/vector-store/files', methods=['POST'])
def upload_file_to_vector_store():
    if not VECTOR_STORE_ID: return jsonify({"error": "VECTOR_STORE_ID not configured."}), 500
    if 'file' not in request.files: return jsonify({"error": "No file part."}), 400
    file = request.files['file']
    if file.filename == '': return jsonify({"error": "No selected file."}), 400
    try:
        uploaded_file = client.files.create(file=(file.filename, file.read()), purpose='assistants')
        vs_file = client.beta.vector_stores.files.create(vector_store_id=VECTOR_STORE_ID, file_id=uploaded_file.id)
        return jsonify({"success": True, "file_id": vs_file.id, "filename": uploaded_file.filename})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/vector-store/files/<string:file_id>', methods=['DELETE'])
def delete_file_from_vector_store(file_id):
    if not VECTOR_STORE_ID: return jsonify({"error": "VECTOR_STORE_ID not configured."}), 500
    try:
        client.beta.vector_stores.files.delete(vector_store_id=VECTOR_STORE_ID, file_id=file_id)
        client.files.delete(file_id=file_id)
        return jsonify({"success": True})
    except Exception as e: return jsonify({"error": str(e)}), 500

@app.route('/api/sync-chats', methods=['POST'])
def sync_chats_to_vector_store():
    if not VECTOR_STORE_ID:
        return jsonify({"error": "VECTOR_STORE_ID is not configured."}), 500

    try:
        # 1. Fetch all messages from Supabase
        response = supabase.table('chat_messages').select('session_id, message, is_user, created_at').order('created_at').execute()
        messages = response.data

        if not messages:
            return jsonify({"message": "No new messages to sync."}), 200

        # 2. Format messages into a text file content
        formatted_content = ""
        current_session_id = None
        for msg in messages:
            if msg['session_id'] != current_session_id:
                formatted_content += f"\n\n--- НОВ ЧАТ: {msg['session_id']} ---\n"
                current_session_id = msg['session_id']

            sender = "Потребител" if msg['is_user'] else "Асистент"
            formatted_content += f"[{msg['created_at']}] {sender}: {msg['message']}\n"

        # 3. Create a file-like object in memory
        file_content_bytes = formatted_content.encode('utf-8')
        in_memory_file = io.BytesIO(file_content_bytes)

        # 4. Upload this file to OpenAI
        uploaded_file = client.files.create(
            file=("chat_history.txt", in_memory_file),
            purpose='assistants'
        )

        # 5. Attach the file to the Vector Store
        vector_store_file = client.beta.vector_stores.files.create(
            vector_store_id=VECTOR_STORE_ID,
            file_id=uploaded_file.id
        )

        return jsonify({"success": True, "message": f"Successfully synced {len(messages)} messages. File ID: {vector_store_file.id}"})

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    app.run(port=5000, debug=True)