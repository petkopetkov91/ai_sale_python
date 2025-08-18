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
from supabase import create_client, Client

# Load environment variables
load_dotenv()

# --- Initialize Clients ---
app = Flask(__name__)

# OpenAI Client
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
ASSISTANT_ID = os.getenv("OPENAI_ASSISTANT_ID")

# Supabase Client
supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_ANON_KEY")
supabase: Client = create_client(supabase_url, supabase_key)


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

@app.route('/api/threads', methods=['GET'])
def get_threads():
    try:
        response = supabase.table('chat_sessions').select('session_id, created_at').order('created_at', desc=True).execute()
        sessions = response.data

        threads_with_titles = []
        for session in sessions:
            try:
                # Check for the first user message to ensure the chat is not empty
                msg_response = supabase.table('chat_messages').select('message').eq('session_id', session['session_id']).eq('is_user', True).order('created_at', desc=False).limit(1).execute()

                # Only include threads that have at least one user message
                if msg_response.data:
                    first_message = msg_response.data[0]['message']
                    threads_with_titles.append({
                        "id": session['session_id'],
                        "title": first_message,
                        "created_at": session['created_at']
                    })
            except Exception as e:
                # Log the error but don't add a broken item to the list
                print(f"Error processing thread {session.get('session_id', 'N/A')}: {e}")

        return jsonify(threads_with_titles)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "Failed to retrieve threads from database."}), 500

@app.route('/api/threads/<string:thread_id>/messages', methods=['GET'])
def get_thread_messages(thread_id):
    try:
        response = supabase.table('chat_messages').select('message, is_user').eq('session_id', thread_id).order('created_at', desc=False).execute()

        formatted_messages = []
        for msg in response.data:
            formatted_messages.append({
                "role": "user" if msg['is_user'] else "assistant",
                "content": msg['message']
            })
        return jsonify(formatted_messages)
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": "Failed to retrieve messages from database."}), 500

@app.route('/chat', methods=['POST'])
def chat():
    try:
        data = request.json
        thread_id = data.get("thread_id")
        user_message = data.get("message")
        is_new_thread = not thread_id

        if is_new_thread:
            thread = client.beta.threads.create()
            thread_id = thread.id
            supabase.table('chat_sessions').insert({"session_id": thread_id}).execute()

        client.beta.threads.messages.create(thread_id=thread_id, role="user", content=user_message)
        supabase.table('chat_messages').insert({"session_id": thread_id, "message": user_message, "is_user": True}).execute()

        run = client.beta.threads.runs.create(assistant_id=ASSISTANT_ID, thread_id=thread_id)

        while run.status in ['queued', 'in_progress', 'requires_action']:
            run = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
            if run.status == 'requires_action':
                tool_call = run.required_action.submit_tool_outputs.tool_calls[0]
                if tool_call.function.name == "get_available_cars":
                    arguments = json.loads(tool_call.function.arguments)
                    car_data_result = get_available_cars(model_filter=arguments.get('model_filter'))
                    
                    # Save summary to DB before returning
                    supabase.table('chat_messages').insert({"session_id": thread_id, "message": car_data_result['summary'], "is_user": False}).execute()

                    client.beta.threads.runs.submit_tool_outputs(
                        thread_id=thread_id,
                        run_id=run.id,
                        tool_outputs=[{"tool_call_id": tool_call.id, "output": f"Function executed. Found {len(car_data_result['cars'])} cars."}]
                    )

                    return jsonify({
                        "response": car_data_result['summary'],
                        "cars": car_data_result['cars'],
                        "thread_id": thread_id,
                        "is_new_thread": is_new_thread
                    })
            time.sleep(1)

        if run.status == 'completed':
            messages = client.beta.threads.messages.list(thread_id=thread_id, order="desc", limit=1)
            response_text = messages.data[0].content[0].text.value

            supabase.table('chat_messages').insert({"session_id": thread_id, "message": response_text, "is_user": False}).execute()

            return jsonify({"response": response_text, "thread_id": thread_id, "is_new_thread": is_new_thread})
        else:
            error_message = f"Грешка: Обработката спря със статус '{run.status}'."
            return jsonify({"response": error_message, "thread_id": thread_id, "is_new_thread": is_new_thread})

    except Exception as e:
        traceback.print_exc()
        error_message = f"Възникна критична грешка на сървъра: {e}"
        return jsonify({"error": error_message}), 500

#if __name__ == '__main__':
    #app.run(port=5000, debug=True)