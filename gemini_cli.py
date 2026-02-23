import google.generativeai as genai
import os
import sys

# --- Configuration ---
# It's best practice to set your API key as an environment variable
# In your terminal, run: export GOOGLE_API_KEY="YOUR_API_KEY"
try: 
    api_key = os.environ["GOOGLE_API_KEY"]
    genai.configure(api_key=api_key)
except KeyError: 
    print("🚨 Error: GOOGLE_API_KEY environment variable not set.") 
    print("Please set it by running: export GOOGLE_API_KEY='YOUR_API_KEY'")
    sys.exit(1)

# --- Model Initialization ---
model = genai.GenerativeModel('gemini-1.5-flash')

# --- Main Execution ---
if __name__ == "__main__":
    # If arguments are provided, use it as a one-off prompt
    if len(sys.argv) > 1:
        # Join all command-line arguments to form the prompt
        prompt = " ".join(sys.argv[1:])

        print(f"💬 Sending to Gemini: '{prompt}'
")

        try:
            response = model.generate_content(prompt, stream=True)
            print("🤖 Gemini says:")
            for chunk in response:
                print(chunk.text, end="", flush=True)
            print()  # for a final newline
        except Exception as e:
            print(f"An error occurred: {e}")

    else:
        # Otherwise, start an interactive chat session
        print("🤖 Gemini Interactive Chat. Type 'exit' or 'quit' to end.")
        print("----------------------------------------------------")
        chat = model.start_chat(history=[])
        while True:
            try:
                prompt = input("You: ")
                if prompt.lower() in ["exit", "quit"]:
                    print("👋 Goodbye!")
                    break

                response = chat.send_message(prompt, stream=True)
                print("🤖 Gemini: ", end="", flush=True)
                for chunk in response:
                    print(chunk.text, end="", flush=True)
                print() # Newline after Gemini's response
            except KeyboardInterrupt:
                print("
👋 Goodbye!")
                break
            except Exception as e:
                print(f"
An error occurred: {e}")
                break
