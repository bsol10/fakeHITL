import os
from repoagent import parse_message_with_ai

def run_terminal_agent():
    if not os.getenv("GOOGLE_API_KEY"):
        key_input = input("Enter your Gemini API Key: ").strip()
        os.environ["GOOGLE_API_KEY"] = key_input

    print("\n=============================================")
    print("      LlamaIndex Extraction Agent    ")
    print("=============================================")
    print("Paste your details below.")
    print("Type 'SEND' on a new line when finished, or 'exit' to quit.\n")

    while True:
        lines = []
        print("-" * 45)
        print("Enter/Paste Data:")

        while True:
            try:
                line = input()
            except EOFError:
                break

            if line.strip().lower() in ["exit", "quit"]:
                print("Exiting agent session.")
                return

            if line.strip() == "SEND":
                break

            lines.append(line)

        raw_text = "\n".join(lines).strip()

        if not raw_text:
            print("No text provided. Try again.")
            continue

        print("\nExtrapolating data...")
        parsed_data = parse_message_with_ai(raw_text)

        if parsed_data:
            print("\n--- Draft Result ---")
            print(parsed_data.model_dump_json(indent=4))
        else:
            print("\nFailed to extract data.")

        print("\nReady for next instruction.")

if __name__ == "__main__":
    run_terminal_agent()
