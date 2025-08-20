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
VECTOR_STORE_ID = os.getenv("OPENAI_VECTOR_STORE_ID")

# Supabase Client
supabase_url = os.getenv("SUPABASE_URL")
supabase_key = os.getenv("SUPABASE_ANON_KEY")
supabase: Client = create_client(supabase_url, supabase_key)

# Cache for car data to reduce XML fetches
CAR_CACHE = {"timestamp": 0, "cars": []}
CACHE_TTL = 300  # seconds


def parse_price(price_str):
    """Extracts the price in BGN from a string like '35 858,96 € / 70 134,03 лв.'"""
    if not price_str:
        return float('inf')
    
    # Търсим цена в лева
    match = re.search(r'([\d\s,]+)\s*лв', price_str)
    if match:
        try:
            # Премахваме интервали и заменяме запетаи с точки
            price_clean = match.group(1).replace(' ', '').replace(',', '.')
            return float(price_clean)
        except (ValueError, TypeError):
            print(f"DEBUG: Грешка при парсване на цена: {price_str}")
            return float('inf')
    
    print(f"DEBUG: Не е намерена цена в лева в: {price_str}")
    return float('inf')


def fetch_all_cars():
    """Fetches and caches car data from the XML feed."""
    now = time.time()
    if CAR_CACHE["cars"] and now - CAR_CACHE["timestamp"] < CACHE_TTL:
        print("DEBUG: Using cached car data")
        return CAR_CACHE["cars"]

    url = "https://sale.peugeot.bg/ecommerce/fb/product_feed.xml"
    print(f"DEBUG: Fetching XML from: {url}")

    response = requests.get(url, timeout=15)
    response.raise_for_status()
    print(f"DEBUG: XML response status: {response.status_code}")

    root = ET.fromstring(response.content)
    ns = {'g': 'http://base.google.com/ns/1.0'}

    items = root.findall('.//channel/item')
    print(f"DEBUG: Намерени общо {len(items)} елемента в XML")

    all_cars = []
    for item in items:
        availability_elem = item.find('g:availability', ns)
        if availability_elem is not None and availability_elem.text == 'in stock':
            title_elem = item.find('g:title', ns)
            description_elem = item.find('g:description', ns)
            link_elem = item.find('g:link', ns)
            image_elem = item.find('g:image_link', ns)

            title = title_elem.text.strip() if title_elem is not None else "N/A"
            description = description_elem.text.strip() if description_elem is not None else "N/A"
            link = link_elem.text if link_elem is not None else "#"
            image_url = image_elem.text if image_elem is not None else ""

            car_data = {
                "model": title,
                "price": description,
                "link": link,
                "image_url": image_url
            }
            all_cars.append(car_data)

    CAR_CACHE["timestamp"] = now
    CAR_CACHE["cars"] = all_cars
    print(f"DEBUG: Събрани данни за {len(all_cars)} автомобила")
    return all_cars


def get_available_cars(model_filter=None):
    """Fetches, filters, sorts by price, and returns the top 2 cheapest cars."""
    print(f"DEBUG: Calling get_available_cars function. Filter: {model_filter}")

    try:
        all_cars = fetch_all_cars()
        print(f"DEBUG: Общо налични автомобили: {len(all_cars)}")

        # Филтриране по модел ако е зададен
        if model_filter:
            print(f"DEBUG: Филтриране по модел: {model_filter}")
            filtered_cars = [car for car in all_cars if model_filter.lower() in car['model'].lower()]
            print(f"DEBUG: След филтриране останаха {len(filtered_cars)} автомобила")
        else:
            filtered_cars = list(all_cars)

        # Добавяме числова цена за сортиране без да променяме кешираните данни
        processed_cars = []
        for car in filtered_cars:
            car_copy = car.copy()
            car_copy['numeric_price'] = parse_price(car_copy['price'])
            print(f"DEBUG: {car_copy['model']} -> numeric_price: {car_copy['numeric_price']}")
            processed_cars.append(car_copy)

        # Сортираме по цена
        sorted_cars = sorted(processed_cars, key=lambda x: x['numeric_price'])

        # Вземаме първите 2
        final_cars = [
            {k: v for k, v in car.items() if k != 'numeric_price'}
            for car in sorted_cars[:2]
        ]

        print(f"DEBUG: Финални {len(final_cars)} автомобила за връщане")

        if not final_cars:
            if model_filter:
                summary = f"За съжаление, в момента няма налични автомобили, отговарящи на вашето търсене за '{model_filter}'."
            else:
                summary = "За съжаление, в момента няма налични автомобили."
            
            print(f"DEBUG: Няма намерени автомобили. Summary: {summary}")
            return {"summary": summary, "cars": []}
        
        if model_filter:
            summary = f"Ето налични автомобили {model_filter}:"
        else:
            summary = "Ето налични автомобили:"
        
        # Премахваме numeric_price от финалния резултат
        for car in final_cars:
            car.pop('numeric_price', None)
        
        result = {"summary": summary, "cars": final_cars}
        print(f"DEBUG: Връщам резултат с {len(result['cars'])} автомобила")
        return result

    except requests.RequestException as e:
        print(f"ERROR: Мрежова грешка: {e}")
        traceback.print_exc()
        summary = "Възникна грешка при свързването с уебсайта на Peugeot."
        return {"summary": summary, "cars": []}
    
    except ET.ParseError as e:
        print(f"ERROR: Грешка при парсване на XML: {e}")
        traceback.print_exc()
        summary = "Възникна грешка при обработката на данните за автомобили."
        return {"summary": summary, "cars": []}
    
    except Exception as e:
        print(f"ERROR: Неочаквана грешка: {e}")
        traceback.print_exc()
        summary = "Възникна грешка при извличането на данните за автомобили."
        return {"summary": summary, "cars": []}

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/admin')
def admin():
    return render_template('admin.html')


@app.route('/api/admin/files', methods=['GET'])
def list_admin_files():
    try:
        if not VECTOR_STORE_ID:
            return jsonify([])
        files = client.beta.vector_stores.files.list(vector_store_id=VECTOR_STORE_ID)
        results = []
        for f in files.data:
            file_id = getattr(f, 'file_id', None) or getattr(f, 'id', None)
            try:
                info = client.files.retrieve(file_id)
                results.append({
                    'id': info.id,
                    'filename': info.filename,
                    'bytes': info.bytes
                })
            except Exception:
                results.append({'id': file_id, 'filename': 'unknown', 'bytes': 0})
        return jsonify(results)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/files', methods=['POST'])
def upload_admin_file():
    if 'file' not in request.files:
        return jsonify({'error': 'No file provided'}), 400
    file = request.files['file']
    try:
        uploaded = client.files.create(file=file, purpose='assistants')
        client.beta.vector_stores.files.create(
            vector_store_id=VECTOR_STORE_ID,
            file_id=uploaded.id
        )
        return jsonify({'id': uploaded.id}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/admin/files/<file_id>', methods=['DELETE'])
def delete_admin_file(file_id):
    try:
        client.beta.vector_stores.files.delete(
            vector_store_id=VECTOR_STORE_ID,
            file_id=file_id
        )
        client.files.delete(file_id)
        return jsonify({'status': 'deleted'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

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

        print(f"DEBUG: Chat request - thread_id: {thread_id}, is_new: {is_new_thread}")

        if is_new_thread:
            thread = client.beta.threads.create()
            thread_id = thread.id
            print(f"DEBUG: Created new thread: {thread_id}")
            supabase.table('chat_sessions').insert({"session_id": thread_id}).execute()

        # Добавяме съобщението на потребителя
        client.beta.threads.messages.create(thread_id=thread_id, role="user", content=user_message)
        supabase.table('chat_messages').insert({"session_id": thread_id, "message": user_message, "is_user": True}).execute()

        # Стартираме run
        tools = [
            {"type": "file_search"},
            {
                "type": "function",
                "function": {
                    "name": "get_available_cars",
                    "description": "Извлича списък с налични автомобили от XML фийд. Използвай тази функция, ако потребителят пита за налични коли, цени или конкретни модели.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "model_filter": {
                                "type": "string",
                                "description": "Модел на автомобил за филтриране (напр. '208', '3008')."
                            }
                        },
                        "required": []
                    }
                }
            }
        ]

        instructions = """
        Ти си полезен асистент на Peugeot.
        - За въпроси относно налични автомобили, цени или конкретни модели, ВИНАГИ използвай инструмента `get_available_cars`.
        - За ВСИЧКИ ДРУГИ въпроси (напр. относно услуги, гаранции, политики, технически спецификации), първо потърси отговор в предоставените файлове чрез твоя `file_search` инструмент.
        - Отговаряй на български език.
        """

        run = client.beta.threads.runs.create(
            assistant_id=ASSISTANT_ID,
            thread_id=thread_id,
            tools=tools,
            instructions=instructions
        )
        print(f"DEBUG: Started run: {run.id}")
        
        car_data_result = None  # За съхранение на резултата от функцията
        max_iterations = 30  # Максимум 30 секунди
        iteration = 0

        while run.status in ['queued', 'in_progress', 'requires_action'] and iteration < max_iterations:
            run = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
            print(f"DEBUG: Run status: {run.status} (iteration {iteration})")
            
            if run.status == 'requires_action':
                print(f"DEBUG: Function call required")
                tool_outputs = []
                
                for tool_call in run.required_action.submit_tool_outputs.tool_calls:
                    print(f"DEBUG: Processing tool call: {tool_call.function.name}")
                    
                    if tool_call.function.name == "get_available_cars":
                        arguments = json.loads(tool_call.function.arguments)
                        print(f"DEBUG: Function arguments: {arguments}")
                        
                        car_data_result = get_available_cars(model_filter=arguments.get('model_filter'))
                        
                        tool_outputs.append({
                            "tool_call_id": tool_call.id,
                            "output": json.dumps(car_data_result, ensure_ascii=False)
                        })

                # Изпращаме резултатите обратно към Assistant-а
                print(f"DEBUG: Submitting tool outputs")
                client.beta.threads.runs.submit_tool_outputs(
                    thread_id=thread_id,
                    run_id=run.id,
                    tool_outputs=tool_outputs
                )
                
            iteration += 1
            time.sleep(1)

        print(f"DEBUG: Run completed with status: {run.status}")

        if run.status == 'completed':
            # Получаваме финалния отговор от Assistant-а
            messages = client.beta.threads.messages.list(thread_id=thread_id, order="desc", limit=1)
            response_text = messages.data[0].content[0].text.value
            print(f"DEBUG: Assistant response: {response_text[:100]}...")

            # Ако имаме данни за коли, показваме кратко описание
            if car_data_result and car_data_result.get('cars'):
                response_text = car_data_result.get('summary', "Ето налични автомобили:")

            # Записваме отговора в базата
            supabase.table('chat_messages').insert({
                "session_id": thread_id,
                "message": response_text,
                "is_user": False
            }).execute()

            # Ако имаме данни за коли, ги включваме в отговора
            response_data = {
                "response": response_text,
                "thread_id": thread_id,
                "is_new_thread": is_new_thread
            }

            if car_data_result and car_data_result.get('cars'):
                response_data["cars"] = car_data_result['cars']
                print(f"DEBUG: Including {len(car_data_result['cars'])} cars in response")

            return jsonify(response_data)
            
        elif run.status == 'failed':
            error_message = f"Грешка: Обработката неуспешна. Причина: {run.last_error.message if run.last_error else 'Неизвестна грешка'}"
            print(f"DEBUG: Run failed: {error_message}")
            return jsonify({"response": error_message, "thread_id": thread_id, "is_new_thread": is_new_thread})
            
        else:
            error_message = f"Грешка: Обработката спря със статус '{run.status}' след {iteration} итерации."
            print(f"DEBUG: Run ended with unexpected status: {run.status}")
            return jsonify({"response": error_message, "thread_id": thread_id, "is_new_thread": is_new_thread})

    except Exception as e:
        print(f"ERROR: Critical server error: {e}")
        traceback.print_exc()
        error_message = f"Възникна критична грешка на сървъра: {e}"
        return jsonify({"error": error_message}), 500

#if __name__ == '__main__':
    #app.run(port=5000, debug=True)
