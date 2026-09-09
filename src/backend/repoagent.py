import os
from llama_index.readers.github import GithubRepositoryReader, GithubClient
from llama_cloud_services import LlamaCloudIndex

def main():
    llama_key = os.getenv("llx-...0dCz")
    github_token = os.getenv("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")
    
    if not llama_key:
        raise ValueError("LLAMA_CLOUD_API_KEY environment variable is missing.")

    print("Connecting to GitHub...")
    github_client = GithubClient(github_token=github_token)

    owner = "run-llama"
    repo = "llama_index"
    branch = "main"

    print(f"Loading repository files from {owner}/{repo}...")
    reader = GithubRepositoryReader(
        github_client=github_client,
        owner=owner,
        repo=repo,
        use_parser=False,
        verbose=True,
        filter_file_extensions=[".md", ".py"],  # Index Python code & Markdown documentation
    )
    
    documents = reader.load_data(branch=branch)
    print(f"Loaded {len(documents)} document nodes.")

    # 2. Push documents and create index via LlamaCloud API
    print("Uploading & indexing with LlamaCloud...")
    index = LlamaCloudIndex.from_documents(
        documents,
        name="run_llama_index_repo",
        project_name="default",
        api_key=llama_key,
        verbose=True
    )

    query_engine = index.as_query_engine()

    print("\n==========================================")
    print(" LlamaIndex GitHub AI Agent is Running ")
    print("==========================================")
    print("Type 'exit' or 'quit' to end the session.\n")

    while True:
        try:
            user_input = input("\nYou: ")
            if user_input.strip().lower() in ["exit", "quit"]:
                print("Exiting AI Agent terminal session.")
                break

            if not user_input.strip():
                continue

            print("\nAI Agent is thinking...")
            response = query_engine.query(user_input)
            print(f"\nAI: {response}")

        except KeyboardInterrupt:
            print("\nSession interrupted. Exiting.")
            break

if __name__ == "__main__":
    main()
