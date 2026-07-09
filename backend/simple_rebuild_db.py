#!/usr/bin/env python3
"""
Simple script to rebuild ChromaDB with individual obligations.
"""

import json
import shutil
from pathlib import Path

def main():
    print("REBUILDING CHROMADB WITH INDIVIDUAL OBLIGATIONS")
    print("=" * 60)
    
    # Find latest consolidated file
    output_dir = Path("output")
    consolidated_files = list(output_dir.glob("*_consolidated.json"))
    
    if not consolidated_files:
        print("ERROR: No consolidated JSON files found")
        return
    
    latest_file = max(consolidated_files, key=lambda f: f.stat().st_mtime)
    print(f"Using file: {latest_file.name}")
    
    # Load data
    with open(latest_file, 'r', encoding='utf-8') as f:
        consolidated_data = json.load(f)
    
    document_name = consolidated_data.get("document_name", "Unknown")
    results = consolidated_data.get("results", [])
    total_obligations = sum(len(cat.get("obligations", [])) for cat in results)
    
    print(f"Document: {document_name}")
    print(f"Categories: {len(results)}")
    print(f"Total obligations: {total_obligations}")
    
    # Backup old database
    chroma_db_path = Path("output/chroma_db")
    backup_path = Path("output/chroma_db_old")
    
    if chroma_db_path.exists():
        if backup_path.exists():
            shutil.rmtree(backup_path)
        try:
            chroma_db_path.rename(backup_path)
            print("Backed up old database")
        except Exception as e:
            print(f"Could not backup database: {e}")
            print("Please stop the API server and try again")
            return
    
    # Create new database
    try:
        from obligation_keywords import (
            flatten_obligations_for_individual_indexing,
            process_consolidated_results_with_auto_keywords
        )
        from vector_store import index_individual_obligations
        
        print("Processing with auto-generated keywords...")
        enhanced_results = process_consolidated_results_with_auto_keywords(results)
        
        print("Flattening to individual obligations...")
        individual_obligations = flatten_obligations_for_individual_indexing(enhanced_results)
        
        print(f"Indexing {len(individual_obligations)} individual obligations...")
        indexed_count = index_individual_obligations(
            document_name=document_name,
            individual_obligations=individual_obligations,
            chroma_path="output/chroma_db"
        )
        
        print(f"SUCCESS: Indexed {indexed_count} individual obligations")
        
        # Verify
        import chromadb
        client = chromadb.PersistentClient(path="output/chroma_db")
        collections = client.list_collections()
        
        print("\nNew database collections:")
        for collection in collections:
            count = collection.count()
            print(f"  {collection.name}: {count} items")
        
        collection_names = [c.name for c in collections]
        if "individual_obligations" in collection_names:
            print("\nSUCCESS: Individual obligations collection created!")
            print("Your query system will now use fine-grained retrieval.")
        
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    main()