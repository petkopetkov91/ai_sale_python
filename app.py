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

# Load environment variables
load_dotenv()

# Initialize Flask app and OpenAI client
app = Flask(__name__)
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
ASSISTANT_ID = os.getenv("OPENAI_ASSISTANT_ID")

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

        if not thread_id:
            thread = client.beta.threads.create()
            thread_id = thread.id

        client.beta.threads.messages.create(thread_id=thread_id, role="user", content=user_message)
        run = client.beta.threads.runs.create(assistant_id=ASSISTANT_ID, thread_id=thread_id)
        print(f"DEBUG: Created run {run.id}. Waiting...")

        while run.status in ['queued', 'in_progress', 'requires_action']:
            run = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
            print(f"DEBUG: Current run status: {run.status}")

            if run.status == 'requires_action':
                tool_call = run.required_action.submit_tool_outputs.tool_calls[0]
                if tool_call.function.name == "get_available_cars":
                    
                    # 1. Get the actual car data
                    arguments = json.loads(tool_call.function.arguments)
                    model_name = arguments.get('model_filter')
                    car_data_result = get_available_cars(model_filter=model_name)
                    
                    # 2. Submit a simple confirmation to OpenAI to close the run
                    try:
                        client.beta.threads.runs.submit_tool_outputs(
                            thread_id=thread_id,
                            run_id=run.id,
                            tool_outputs=[{
                                "tool_call_id": tool_call.id,
                                "output": f"Function executed. Found {len(car_data_result['cars'])} cars."
                            }]
                        )
                    except Exception as e:
                        print(f"WARNING: Could not submit dummy tool output to close run: {e}")

                    # 3. Immediately return the real data to the frontend
                    return jsonify({
                        "response": car_data_result['summary'],
                        "cars": car_data_result['cars'],
                        "thread_id": thread_id
                    })
            
            time.sleep(1)

        # This part handles general queries (from Retrieval)
        if run.status == 'completed':
            messages = client.beta.threads.messages.list(thread_id=thread_id, order="desc", limit=1)
            response_text = messages.data[0].content[0].text.value
            return jsonify({"response": response_text, "thread_id": thread_id})
        else:
            return jsonify({"response": f"Грешка: Обработката спря със статус '{run.status}'.", "thread_id": thread_id})

    except openai.BadRequestError as e:
        print(f"ERROR: Caught a BadRequestError: {e}")
        return jsonify({"error": "Системата все още обработва предишната заявка. Моля, изчакайте и опитайте отново."}), 429
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": f"Възникна критична грешка на сървъра: {e}"}), 500

#if __name__ == '__main__':
 #   app.run(port=5000, debug=True)