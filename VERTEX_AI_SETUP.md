# Using a Vertex AI API Key

If your team gave you a **Vertex AI (Google Cloud) API key** instead of a Google AI Studio key, the app can call Vertex AI with that key.

## Option A: Vertex AI express mode (no project ID)

If your key was created in **express mode** (e.g. from [Express Mode](https://console.cloud.google.com/expressmode)), the project is already tied to the key. You **do not** need to set a project ID.

```env
GOOGLE_GENAI_USE_VERTEXAI=True
GEMINI_API_KEY=your_vertex_express_mode_api_key_here
# Do NOT set GOOGLE_CLOUD_PROJECT — express mode uses the key’s project
```

## Option B: Vertex AI with project/location

If you have a normal GCP project and a Vertex API key for it, set project (and optionally location):

```env
GOOGLE_GENAI_USE_VERTEXAI=True
GEMINI_API_KEY=your_vertex_api_key_here
GOOGLE_CLOUD_PROJECT=your-gcp-project-id
# Optional; default is us-central1
# VERTEX_LOCATION=us-central1
```

You can use `VERTEX_PROJECT` instead of `GOOGLE_CLOUD_PROJECT`, and `VERTEX_LOCATION` instead of `GOOGLE_CLOUD_LOCATION`.

## 2. Model name

`GEMINI_MODEL` (e.g. `gemini-2.5-flash-lite` or `gemini-2.5-flash`) is the short model name. The client builds the full Vertex path:  
`projects/{project}/locations/{location}/publishers/google/models/{model}`.

## 3. How it’s called

- **Vertex AI (API key)**  
  - Endpoint: `https://aiplatform.googleapis.com/v1/projects/{project}/locations/{location}/publishers/google/models/{model}:generateContent`  
  - Auth: API key in query string: `?key=...`

- **Google AI Studio (Gemini API)**  
  - Endpoint: `https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent`  
  - Auth: API key in header: `x-goog-api-key`

If you use a Vertex key on the AI Studio endpoint (or the other way around), you get 401 “API keys are not supported” or “Expected OAuth2”. Setting `GOOGLE_GENAI_USE_VERTEXAI=True` and `GOOGLE_CLOUD_PROJECT` makes the app use the Vertex endpoint with your key.
