import os
from openai import OpenAI
from dotenv import load_dotenv

def main():
    """
    This script performs a one-time update to permanently link a Vector Store
    to an OpenAI Assistant.
    """
    load_dotenv()

    print("Loading environment variables...")
    api_key = os.getenv("OPENAI_API_KEY")
    assistant_id = os.getenv("ASSISTANT_ID")
    vector_store_id = os.getenv("VECTOR_STORE_ID")

    if not all([api_key, assistant_id, vector_store_id]):
        print("\nERROR: Missing one or more required environment variables.")
        print("Please ensure OPENAI_API_KEY, ASSISTANT_ID, and VECTOR_STORE_ID are set in your .env file.")
        return

    print(f"Assistant ID: {assistant_id}")
    print(f"Vector Store ID: {vector_store_id}")

    try:
        client = OpenAI(api_key=api_key)

        print("\nUpdating assistant...")

        # This is the core operation: permanently attaching the vector store
        # to the assistant for the file_search tool.
        assistant = client.beta.assistants.update(
            assistant_id=assistant_id,
            tool_resources={"file_search": {"vector_store_ids": [vector_store_id]}},
        )

        print("\n✅ SUCCESS!")
        print(f"Assistant '{assistant.name}' (ID: {assistant.id}) has been successfully updated.")
        print("It is now permanently linked to the specified Vector Store.")
        print("The main application no longer needs to pass 'tool_resources' on every run.")

    except Exception as e:
        print(f"\n❌ FAILED: An error occurred.")
        print(f"Error details: {e}")

if __name__ == "__main__":
    main()
