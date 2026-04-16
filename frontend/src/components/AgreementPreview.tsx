import { useParams, useNavigate } from 'react-router-dom';
import { motion } from 'motion/react';
import { ArrowLeft, FileText, Building2, FileCheck, File, Trash2 } from 'lucide-react';
import { Button } from './ui/button';
import { Tabs, TabsContent, TabsList, TabsTrigger } from './ui/tabs';
import type { Agreement } from './agreementModel';
import { mockAgreements } from './agreementModel';
import { getAgreementById, deleteAgreement, deleteMockAgreement } from '../utils/agreementStorage';
import { useState } from 'react';
import { ALL_DOCUMENTS as GENERATED_ALL_DOCUMENTS } from '../generated/documents';

// From public/docs + legacy ids for backward compat with saved agreements
const LEGACY_DOCUMENTS = [
  { id: 'doc-1', name: 'Commercial Lease Agreement - Buyer Triple Net.pdf' },
];
const ALL_DOCUMENTS = [...GENERATED_ALL_DOCUMENTS, ...LEGACY_DOCUMENTS];

// Helper function to get file type icon
const getFileIcon = (fileName: string) => {
  const extension = fileName.split('.').pop()?.toLowerCase();
  switch (extension) {
    case 'pdf':
      return <FileText className="w-5 h-5 text-red-600" />;
    case 'doc':
    case 'docx':
      return <FileText className="w-5 h-5 text-blue-600" />;
    case 'xls':
    case 'xlsx':
      return <FileText className="w-5 h-5 text-green-600" />;
    default:
      return <File className="w-5 h-5 text-gray-600" />;
  }
};

export function AgreementPreview() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  
  // Try to find agreement in storage first (to get latest saved data), then in mock data
  const storedAgreement = id ? getAgreementById(id) : undefined;
  const mockAgreement = mockAgreements.find((a) => a.id === id);
  // Prioritize stored agreement over mock (stored has latest saved documents)
  const agreement = storedAgreement || mockAgreement;

  if (!agreement) {
    return (
      <div className="flex min-h-[50vh] flex-col items-center justify-center bg-gray-50 px-4">
        <div className="text-center">
          <h2 className="text-lg font-semibold text-gray-900">Agreement not found</h2>
          <Button onClick={() => navigate('/agreements')} className="mt-4">
            Back to land details
          </Button>
        </div>
      </div>
    );
  }

  const handleEdit = () => {
    navigate(`/agreements/${id}/edit`);
  };

  const handleDelete = () => {
    if (id) {
      if (storedAgreement) {
        // Delete stored agreement
        const success = deleteAgreement(id);
        if (success) {
          navigate('/agreements');
        }
      } else if (mockAgreement) {
        // Delete mock agreement (mark as deleted)
        const success = deleteMockAgreement(id);
        if (success) {
          navigate('/agreements');
        }
      }
    }
    setShowDeleteConfirm(false);
  };

  const handleDeleteClick = () => {
    // Show confirmation for all agreements
    setShowDeleteConfirm(true);
  };

  const handleCancelDelete = () => {
    setShowDeleteConfirm(false);
  };

  return (
    <div className="flex min-h-full flex-col bg-gray-50">
      <div className="border-b border-gray-200 bg-white px-6 py-3">
        <button
          type="button"
          onClick={() => navigate('/agreements')}
          className="flex items-center gap-2 text-sm font-medium text-gray-700 transition-colors hover:text-gray-900"
        >
          <ArrowLeft className="h-4 w-4" />
          Back to land details
        </button>
      </div>

      <div className="flex-1 bg-gray-50">
        {/* Page Header - Match landing page structure */}
        <div className="bg-white px-6 py-6 border-b border-gray-200">
          <div className="max-w-7xl mx-auto">
            <div className="flex items-center justify-between mb-6" style={{ marginBottom: '20px' }}>
              <h1 className="text-3xl font-bold text-gray-900">Agreement details</h1>
              <button
                onClick={handleEdit}
                className="bg-white hover:bg-gray-50 text-blue-600 border-2 border-blue-600 px-6 py-2 rounded-full font-medium text-sm transition-colors flex items-center gap-2"
              >
                <FileCheck className="w-4 h-4" />
                Edit Agreement
              </button>
            </div>
            <motion.div
              initial={{ opacity: 0, y: -10 }}
              animate={{ opacity: 1, y: 0 }}
              className="text-sm text-gray-600"
            >
              <div className="flex items-center gap-4 flex-wrap">
                <div className="flex items-center gap-2">
                  <span className="font-medium text-gray-900">Agreement ID:</span>
                  <span>{agreement.agreementNumber}</span>
                </div>
                <span className="text-gray-400">•</span>
                <div className="flex items-center gap-2">
                  <span className="font-medium text-gray-900">Name:</span>
                  <span>{agreement.name}</span>
                </div>
              </div>
            </motion.div>
          </div>
        </div>

        {/* Tabs */}
        <div className="max-w-7xl mx-auto px-6 py-6">
          <Tabs defaultValue="information" className="w-full">
            <TabsList className="mb-6 w-full justify-start overflow-x-auto">
              <TabsTrigger 
                value="information" 
                className="gap-2 whitespace-nowrap text-sm data-[state=active]:bg-gray-200 data-[state=active]:text-gray-900"
              >
                <FileText className="w-4 h-4" />
                Information
              </TabsTrigger>
              <TabsTrigger 
                value="documents" 
                className="gap-2 whitespace-nowrap text-sm data-[state=active]:bg-gray-200 data-[state=active]:text-gray-900"
              >
                <Building2 className="w-4 h-4" />
                Documents
              </TabsTrigger>
            </TabsList>

            <TabsContent value="information">
              <motion.div
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                transition={{ delay: 0.1 }}
                className="space-y-6"
              >
                {/* Identification Section - Match landing page typography (text-sm) */}
                <div className="bg-white border border-gray-200 rounded-lg p-6">
                  <h2 className="text-base font-semibold text-gray-900 mb-4 pb-4 border-b border-gray-200">Identification</h2>
                  
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-x-12 gap-y-4">
                    <div>
                      <label className="block mb-1.5 text-sm font-medium text-gray-700">Agreement name</label>
                      <p className="text-gray-900 text-sm">{agreement.name}</p>
                      <div style={{height:'10px'}}></div>
                    </div>
                    
                    <div>
                      <label className="block mb-1.5 text-sm font-medium text-gray-700">Agreement date</label>
                      <p className="text-gray-900 text-sm">{agreement.date}</p>
                    </div>
                  </div>

                  {agreement.notes && (
                    <div className="mt-4">
                      <label className="block mb-1.5 text-sm font-medium text-gray-700">Notes</label>
                      <p className="text-gray-900 text-sm leading-relaxed">{agreement.notes}</p>
                    </div>
                  )}
                </div>

                {/* Maintenance Section - Only show if exists - Match landing page typography */}
                {agreement.maintenance && (
                  <motion.div
                    initial={{ opacity: 0, y: 20 }}
                    animate={{ opacity: 1, y: 0 }}
                    transition={{ delay: 0.2 }}
                    className="bg-white border border-gray-200 rounded-lg p-6"
                  >
                    <h2 className="text-base font-semibold text-gray-900 mb-4 pb-4 border-b border-gray-200">Maintenance</h2>
                    
                    <div className="space-y-4">
                      <div>
                        <label className="block mb-1.5 text-sm font-medium text-gray-700">Responsible party</label>
                        <p className="text-gray-900 text-sm">{agreement.maintenance.responsibleParty}</p>
                      </div>
                      
                      <div>
                        <label className="block mb-1.5 text-sm font-medium text-gray-700">
                          Maintenance owner responsibility
                        </label>
                        <p className="text-gray-900 text-sm leading-relaxed">
                          {agreement.maintenance.ownerResponsibility}
                        </p>
                      </div>
                      
                      <div>
                        <label className="block mb-1.5 text-sm font-medium text-gray-700">Maintenance reasoning</label>
                        <p className="text-gray-900 text-sm leading-relaxed">{agreement.maintenance.reasoning}</p>
                      </div>
                    </div>
                  </motion.div>
                )}
              </motion.div>
            </TabsContent>

            <TabsContent value="documents">
              <div className="bg-white border border-gray-200 rounded-lg p-6">
                {agreement.documents && agreement.documents.length > 0 ? (
                  <div className="space-y-3">
                    {agreement.documents.map((docId) => {
                      const doc = ALL_DOCUMENTS.find(d => d.id === docId);
                      if (!doc) return null;
                      return (
                        <div
                          key={doc.id}
                          className="flex items-center gap-3 p-3 border border-gray-200 rounded-lg hover:bg-gray-50 transition-colors"
                        >
                          {getFileIcon(doc.name)}
                          <span className="text-gray-900 font-medium text-sm">{doc.name}</span>
                        </div>
                      );
                    })}
                  </div>
                ) : (
                  <p className="text-gray-600 text-sm text-center py-8">No documents attached to this agreement.</p>
                )}
              </div>
            </TabsContent>
          </Tabs>

          {/* Delete Button - At the bottom */}
          <div className="mt-16 pb-8 border-t-2 border-gray-300 pt-10" style={{paddingTop:24}}>
            <button
              onClick={handleDeleteClick}
              className="bg-white hover:bg-gray-50 text-red-600 border-2 border-red-600 px-6 py-2 rounded-full font-medium text-sm transition-colors flex items-center gap-2"
            >
              <Trash2 className="w-4 h-4" />
              Delete Agreement
            </button>
          </div>
        </div>
      </div>

      {/* Delete Confirmation Modal */}
      {showDeleteConfirm && (
        <div className="fixed inset-0 z-50 flex items-center justify-center p-4 bg-black/50">
          <div className="bg-white rounded-lg shadow-xl max-w-md w-full p-6">
            <h3 className="text-lg font-semibold text-gray-900 mb-2">Confirm Deletion</h3>
            <p className="text-gray-600 mb-6">
              Are you sure you want to delete this agreement? This action cannot be undone.
            </p>
            <div className="flex items-center justify-end gap-3">
              <Button
                onClick={handleCancelDelete}
                variant="outline"
                className="px-4 py-2"
              >
                Cancel
              </Button>
              <Button
                onClick={handleDelete}
                className="bg-red-600 hover:bg-red-700 text-white px-4 py-2"
              >
                Delete
              </Button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
