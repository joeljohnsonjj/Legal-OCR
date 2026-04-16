// Utility to generate PDF content as base64
// In a real app, these would be actual PDF files stored on the server

export const PDF_DOCUMENTS: Record<string, string> = {
  // PDF 1: Owner Responsibility - Structural & Systems (Page 12)
  'snippet-1': generatePDF1(),
  // PDF 2: Triple Net Lease (Page 8)
  'snippet-2': generatePDF2(),
  // PDF 3: Shared Responsibility (Page 5)
  'snippet-3': generatePDF3(),
  // PDF 4: Full Service Lease (Page 15)
  'snippet-4': generatePDF4(),
  // PDF 5: Percentage Lease (Page 19)
  'snippet-5': generatePDF5(),
};

// Generate a simple PDF-like document structure
// In production, these would be actual PDF files
function generatePDF1(): string {
  // This is a placeholder - in real implementation, this would be a base64 PDF
  // For now, we'll use a data URL approach with react-pdf
  return 'pdf-1';
}

function generatePDF2(): string {
  return 'pdf-2';
}

function generatePDF3(): string {
  return 'pdf-3';
}

function generatePDF4(): string {
  return 'pdf-4';
}

function generatePDF5(): string {
  return 'pdf-5';
}

