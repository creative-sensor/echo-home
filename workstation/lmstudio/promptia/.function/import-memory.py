#!/usr/bin/env python
import os
import re
import uuid
import argparse
import hashlib
from openai import OpenAI

try:
    import chromadb
except ImportError:
    print("❌ ERROR: Missing dependencies. Run: pip install chromadb")
    exit(1)

def parse_args():
    parser = argparse.ArgumentParser(description="Import Neos text memories into ChromaDB.")
    parser.add_argument('--port', type=int, default=8080, help='Port of the local LLM server')
    parser.add_argument('--host', type=str, default='localhost', help='Hostname of the local LLM server')
    parser.add_argument('--dir', type=str, default='neos/docs', help='Directory containing the text memories')
    parser.add_argument('--db-path', type=str, default='./neos_memory/vector', help='Path to the ChromaDB vector storage')
    return parser.parse_args()

def get_document_id(text: str) -> str:
    """Generate a deterministic ID based on content to prevent duplicates."""
    return hashlib.md5(text.encode('utf-8')).hexdigest()

def main():
    args = parse_args()
    
    if not os.path.exists(args.dir):
        print(f"❌ Directory not found: {args.dir}")
        print("Ensure you have run Neos and saved some memories first.")
        return

    # 1. Initialize ChromaDB and OpenAI Client
    print(f"🧠 Connecting to RAG Memory Database at '{args.db_path}'...")
    chroma_client = chromadb.PersistentClient(path=args.db_path)
    collection = chroma_client.get_or_create_collection(name="nvim_sessions")
    
    print(f"🔗 Connecting to embedding endpoint at http://{args.host}:{args.port}/v1")
    openai_client = OpenAI(base_url=f"http://{args.host}:{args.port}/v1", api_key="localm")

    files = [f for f in os.listdir(args.dir) if f.endswith(".txt")]
    if not files:
        print(f"⚠️ No text files found in {args.dir}.")
        return

    print(f"\n📂 Found {len(files)} memory files to process...\n")
    
    imported_count = 0
    skipped_count = 0

    # 2. Process each file
    for filename in files:
        filepath = os.path.join(args.dir, filename)
        
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
            
        # Extract Intent
        intent_match = re.search(r"^Intent:\s*(.*)$", content, re.MULTILINE)
        if not intent_match:
            print(f"⚠️ Skipping {filename}: Could not find 'Intent:' line.")
            continue
        user_intent = intent_match.group(1).strip()
        
        # Extract Summary (Everything between "Summary:" and the next "=========")
        summary_match = re.search(r"Summary:\n(.*?)\n={50}", content, re.DOTALL)
        if not summary_match:
            print(f"⚠️ Skipping {filename}: Could not find 'Summary:' section.")
            continue
        summary = summary_match.group(1).strip()
        
        # Format document exactly as Neos does
        document_text = f"Original Intent: {user_intent}\nSolution Summary:\n{summary}"
        
        # Use a hash of the text as the ID so we don't insert the exact same memory twice
        doc_id = get_document_id(document_text)
        
        # Check if already exists
        existing = collection.get(ids=[doc_id])
        if existing and existing['ids']:
            print(f"⏭️  Skipping {filename} (Already in database)")
            skipped_count += 1
            continue
            
        print(f"⏳ Embedding {filename}...")
        try:
            # Generate Embedding
            response = openai_client.embeddings.create(
                input=[document_text], 
                model="local-model"
            )
            embedding = response.data[0].embedding
            
            # Insert into Vector DB
            collection.add(
                ids=[doc_id],
                embeddings=[embedding],
                documents=[document_text],
                metadatas=[{"intent": user_intent, "source_file": filename}]
            )
            print(f"✅ Imported: {filename}")
            imported_count += 1
            
        except Exception as e:
            print(f"❌ Failed to embed/store {filename}: {e}")

    print("\n" + "="*40)
    print("🎉 Import Complete!")
    print(f"   Successfully imported: {imported_count}")
    print(f"   Skipped (Duplicates):  {skipped_count}")
    print("="*40 + "\n")

if __name__ == "__main__":
    main()
