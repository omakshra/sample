﻿from flask import Flask, request, jsonify
from flask_cors import CORS
from sqlalchemy import create_engine, inspect, text
import spacy
import difflib
import json
import os
import requests 
import threading
import time
import logging
from spacy.pipeline import EntityRuler

app = Flask(__name__)
CORS(app)
logging.basicConfig(level=logging.INFO)
# -------- CONFIGURE SQL SERVER CONNECTION --------
DATABASE_URL = (
    "mssql+pyodbc://@localhost/PoliceRMS"
    "?driver=ODBC+Driver+17+for+SQL+Server"
    "&trusted_connection=yes"
    "&Encrypt=no"
)
cached_db_values = {}
engine = create_engine(DATABASE_URL)
inspector = inspect(engine)

# Load synonym map
with open(os.path.join(os.path.dirname(__file__), "module_synonyms.json")) as f:
    synonym_map = json.load(f)
synonym_to_module = {syn.lower(): module.lower() for module, syns in synonym_map.items() for syn in [module] + syns}

# Load SpaCy model
def load_spacy_model():
    try:
        return spacy.load("en_core_web_md")
    except OSError:
        return spacy.load("en_core_web_sm")

nlp = load_spacy_model()

# Label normalization
ENTITY_LABEL_MAP = {
    "gpe": "location",
    "person": "name",
    "org": "organization",
    "date": "date",
    "time": "time",
    "cardinal": "age",
    "phone": "phone_number",
    "email": "email"
}
# ---------------- PATTERN BUILDING -------------------
def get_sample_values(table, column, limit=50):
    query = text(f"SELECT DISTINCT {column} FROM {table} WHERE {column} IS NOT NULL")
    with engine.connect() as conn:
        result = conn.execute(query).fetchmany(limit)
    return [str(row[0]) for row in result if row[0]]

def refresh_cached_db_values():
    global cached_db_values
    cached_db_values = {}
    patterns = []

    for table in inspector.get_table_names():
        cached_db_values[table] = {}
        patterns.append({"label": "MODULE", "pattern": table.lower()})

        for col in inspector.get_columns(table):
            col_name = col['name'].lower()
            samples = get_sample_values(table, col['name'])
            cached_db_values[table][col_name] = samples

            for val in samples:
                if val and len(str(val)) > 1:
                    patterns.append({"label": col['name'].upper(), "pattern": val.lower()})

    return patterns

def add_generic_patterns():
    return [
        {"label": "PHONE", "pattern": [{"TEXT": {"REGEX": r"\d{3}-\d{3}-\d{4}"}}]},
        {"label": "EMAIL", "pattern": [{"TEXT": {"REGEX": r".+@.+\..+"}}]},
        {"label": "DATE", "pattern": [{"TEXT": {"REGEX": r"\d{1,2}/\d{1,2}/\d{2,4}"}}]},
        {"label": "NAME", "pattern": [{"TEXT": {"REGEX": r"^[A-Z][a-z]+$"}}]},
    ]

# Add patterns to NER (entity ruler) - keep overwrite_ents=False to keep SpaCy NER too
if "entity_ruler" in nlp.pipe_names:
    nlp.remove_pipe("entity_ruler")
ruler = nlp.add_pipe("entity_ruler", before="ner", config={"overwrite_ents": False})
ruler.add_patterns(refresh_cached_db_values() + add_generic_patterns())

# -------------------- AUTO REFRESH THREAD --------------------
def auto_refresh_cache(interval_minutes=10):
    while True:
        print("[AutoRefresh] Refreshing DB cache...")
        refresh_cached_db_values()
        time.sleep(interval_minutes * 60)

threading.Thread(target=auto_refresh_cache, daemon=True).start()

# ------------------ NLP UTILITIES ----------------
def extract_intent(text):
    text = text.lower()
    if any(x in text for x in ['search', 'find', 'lookup', 'display', 'show', 'get', 'fetch', 'list']):
        return "search"
    if any(x in text for x in ['add', 'insert', 'create']):
        return "add"
    if any(x in text for x in ['update', 'modify', 'change']):
        return "update"
    if any(x in text for x in ['delete', 'remove']):
        return "delete"
    return "unknown"

def extract_module(text, table_names):
    text = text.lower()
    table_names_lower = [t.lower() for t in table_names]
    doc = nlp(text)
    # Direct match
    for table in table_names_lower:
        if table in text:
            return table
    # Exact or fuzzy synonym match
    for word in text.split():
        if word in synonym_to_module:
            return synonym_to_module[word]
        matches = difflib.get_close_matches(word, synonym_to_module.keys(), n=1, cutoff=0.8)
        if matches:
            return synonym_to_module[matches[0]]
    # Semantic similarity for module names
    for table in table_names_lower:
        table_doc = nlp(table)
        sim_scores = [token.similarity(table_doc) for token in doc if token.has_vector and table_doc.has_vector]
        if sim_scores and max(sim_scores) >= 0.7:
            return table
    # Fuzzy fallback
    fuzzy_matches = difflib.get_close_matches(text, table_names_lower, n=1, cutoff=0.75)
    if fuzzy_matches:
        return fuzzy_matches[0]
    return "general"

def fix_gpe_to_person(ents):
    fixed_ents = []
    for ent in ents:
        if ent.label_ == "GPE" and ent.text.istitle() and len(ent.text.split()) == 1:
            fixed_ents.append((ent.text, "PERSON"))
        else:
            fixed_ents.append((ent.text, ent.label_))

    return fixed_ents

def fuzzy_match_entity_to_db(entity_text, candidates, cutoff=0.7):

    if not candidates:
        return None
    # Use difflib first for quick match
    matches = difflib.get_close_matches(entity_text.lower(), [c.lower() for c in candidates], n=1, cutoff=cutoff)
    if matches:
        return matches[0]
    # If no difflib match, fallback to SpaCy similarity
    entity_doc = nlp(entity_text)
    best_score = 0
    best_match = None
    for candidate in candidates:
        candidate_doc = nlp(candidate)
        if entity_doc.has_vector and candidate_doc.has_vector:
            score = entity_doc.similarity(candidate_doc)
            if score > best_score and score >= cutoff:
                best_score = score
                best_match = candidate
    return best_match
# -------------------- STRUCTURED QUERY --------------------
def build_structured_query(command):
    doc = nlp(command)
    # Merge full PERSON/NAME entities into one entry
    full_names = [ent.text for ent in doc.ents if ent.label_ in ("PERSON", "NAME")]
    entities = {}
    if full_names:
        entities["name"] = full_names[0] if len(full_names) == 1 else full_names
    fixed_entities = fix_gpe_to_person(doc.ents)
    table_names = inspector.get_table_names()
    module = None
    for text, label in fixed_entities:
        if label.upper() == "MODULE" and text.lower() in table_names:
            module = text.lower()
            break
    if not module:
        module = extract_module(command, table_names)

    module_db_values = []
    if module in cached_db_values:
        for col_samples in cached_db_values[module].values():
            module_db_values.extend(col_samples)

    entities = {}
    for ent_text, ent_label in fixed_entities:
        raw_label = ent_label.lower()
        if raw_label == "module":
            continue

        norm_label = ENTITY_LABEL_MAP.get(raw_label, raw_label)
        if norm_label not in ['date', 'time', 'phone_number', 'email']:
            matched_value = fuzzy_match_entity_to_db(ent_text, module_db_values)
            if matched_value:
                ent_text = matched_value

        if norm_label in entities:
            if isinstance(entities[norm_label], list):
                if ent_text not in entities[norm_label]:
                    entities[norm_label].append(ent_text)
            else:
                if entities[norm_label] != ent_text:
                    entities[norm_label] = [entities[norm_label], ent_text]
        else:
            entities[norm_label] = ent_text

    intent = extract_intent(command)
    logging.info(f"Extracted intent: {intent}, module: {module}, entities: {entities}")
    return {
        "message": f"Command processed: '{command}'",
        "intent": intent,
        "module": module,
        "entities": entities
    }
# -------------------- ROUTES --------------------
@app.route('/api/command', methods=['POST'])
def handle_command():
    data = request.get_json()
    command = data.get("command", "").strip()
    if not command:
        return jsonify({"error": "No command provided"}), 400

    result = build_structured_query(command)
    return jsonify(result)

if __name__ == '__main__':
    print("Server starting...")
    app.run(debug=True)
