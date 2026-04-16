# PDF Documents Folder

Place your PDF files in this folder (`public/docs/`) for them to be accessible by the application.

## Required PDF Files

Based on your API response, ensure the following PDF files are present:

1. **Commercial Lease Agreement - Buyer Triple Net.pdf**
   - This file should match the `docId` from your `/query` endpoint response
   - The file name must match exactly (including spaces and capitalization)

## File Naming

The PDF file names must match the `docId` values from your backend API response's `Citation` array, with `.pdf` extension added if not already present.

For example:
- If `docId` is: `"Commercial Lease Agreement - Buyer Triple Net"`
- The file should be: `Commercial Lease Agreement - Buyer Triple Net.pdf`

## How It Works

- Vite serves files from the `public` folder at the root URL path
- Files in `public/docs/` are accessible at `/docs/filename.pdf`
- The PDF viewer component automatically constructs the URL based on the `docId` from the API response

## Troubleshooting

If you see "Failed to load PDF" errors:
1. Verify the PDF file exists in this folder
2. Check that the file name matches the `docId` exactly (case-sensitive)
3. Ensure the file has a `.pdf` extension
4. Restart your Vite dev server after adding new files

