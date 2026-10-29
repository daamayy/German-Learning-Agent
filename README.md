# German Learning Agent

Turns German YouTube content into structured, reviewable study material.

**I use this every day.** It is not a demo. It is the tool I actually study with, and it has been shaped by what I found useful and what I didn't.

**Why:** Textbook vocabulary lists don't stick. Context does.
I wanted study material built from content I actually want to watch,
structured the way I actually review.

**Languages:** German is the source language, and all explanations and translations are in English and Chinese.

It has two tools, sharing one `config.json`: **German Context** (a study PDF per video) and **German Vocab** (a growing Excel vocabulary database).

## German Context: what it generates

A study PDF per video, containing:

- **Transcript, aligned with translation**: the spoken German is cleaned up into standard German with minimal edits (every change is recorded), and the numbered lines match the translation.
- **Grammatik**: grammar points in context, not in isolation
- **Wendungen**: high-frequency expressions pulled from the video
- **Redemittel & reusable sentences**: patterns extracted from the source that work outside the video's topic
- **Personalization**: prompts for adapting each pattern to my own situation (daily life, work, interviews)

The last section is the point: recognizing a phrase isn't the same
as being able to use it.

The transcript pages have wide line spacing and side margins, so the PDF is meant to be printed and written on.

## Vocabulary builder

`german_vocab.py` is the second half of the workflow. The PDF is for studying one video in depth; this tool builds a vocabulary database that grows across all videos.

Paste a YouTube link, and it appends the video's vocabulary to a single Excel file. Each entry has:

- **Word**: nouns with article (e.g. *der Bahnhof*), verbs in the infinitive
- **Part of speech** and **CEFR level** (A1 to C1), so I can filter by difficulty
- **Chinese and English meaning**
- **Example sentence**: the original line from the video's transcript, with its translation, so every word stays tied to the context where I met it
- **Particles / collocations**: modal particles like *doch*, *mal*, *eben*, *halt* and fixed phrases, with their tone explained
- **Notes**, **video topic**, **video link** and **date added**

Over time the Excel file becomes a searchable record of everything I've picked up, and where I picked it up.

## Stack

- **LLM**: Google Gemini (with automatic fallback across models when one is overloaded or out of quota)
- **PDF generation**: ReportLab
- **Excel**: openpyxl
- **Transcripts**: `youtube-transcript-api`
- **UI**: Tkinter desktop apps

## Run it

```bash
# Study PDF
pip install youtube-transcript-api google-genai reportlab
python german_context.py

# Vocabulary Excel
pip install youtube-transcript-api google-genai openpyxl
python german_vocab.py
```

Put your Gemini API key in `config.json`:

```json
{ "api_key": "AIza..." }
```

## Note

Personal learning tool. Transcripts are fetched at runtime;
generated materials are not committed or redistributed.
