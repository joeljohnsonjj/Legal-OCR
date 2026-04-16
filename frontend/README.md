# AI Autofill Feature Design

## Setup & Installation

1. **Install dependencies:**
   ```bash
   npm install
   ```

2. **Add PDF documents:**
   - Place your PDF documents in the `public/docs/` folder
   - The app will automatically detect and list them

## Running the Application

### Development Mode
```bash
npm run dev
```
This will:
- Auto-generate the document list from `public/docs/`
- Start the Vite development server
- Open the app at `http://localhost:5173`

### Production Build
```bash
npm run build
```
This will:
- Auto-generate the document list
- Build the optimized production bundle

### Stub Server (Optional)
If the Legal-OCR backend is not available, run the stub server for testing:
```bash
npm run stub-server
```
The stub server provides mock API responses for development and testing.

## Project Structure

```
├── public/docs/          # PDF documents (auto-detected)
├── scripts/              # Build scripts (document generation)
├── server/               # Stub OCR server for testing
├── src/
│   ├── components/       # React components
│   ├── generated/        # Auto-generated files (documents.ts)
│   └── App.tsx          # Main application
└── package.json
```

## Notes

- The `src/generated/` folder is auto-generated - do not edit manually
- Document list is regenerated before each dev/build run
- The stub server runs on port 3001 by default
