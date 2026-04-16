/**
 * API Service Test File
 * 
 * This file contains test examples for the API service.
 * Run these tests to verify backend integration is working correctly.
 */

import { queryObligations, transformObligationToSnippet } from './apiService';
import type { BackendObligation } from './apiService';

/**
 * Test 1: Query Obligations
 * Tests the main query functionality with a sample search
 */
export async function testQueryObligations() {
  console.log('🔍 Testing query obligations...');
  try {
    const query = 'Landlord HVAC Hazardous Materials';
    const response = await queryObligations(query);
    
    console.log('✅ Query successful!');
    console.log(`   Query: "${response.query}"`);
    console.log(`   Documents searched: ${response.total_documents_searched}`);
    console.log(`   Obligations found: ${response.total_obligations_found}`);
    console.log(`   Results count: ${response.results.length}`);
    
    if (response.results.length > 0) {
      console.log('\n   First result:');
      console.log(`   - DutyType: ${response.results[0].DutyType}`);
      console.log(`   - Responsible Party: ${response.results[0]['Responsible Party']}`);
      console.log(`   - Citation: ${JSON.stringify(response.results[0].Citation)}`);
    }
    
    return true;
  } catch (error) {
    console.error('❌ Query failed:', error);
    return false;
  }
}

/**
 * Test 2: Transform Obligation
 * Tests the data transformation logic
 */
export function testTransformObligation() {
  console.log('🔄 Testing obligation transformation...');
  
  const mockObligation: BackendObligation = {
    DutyType: 'Hazardous Materials Indemnification',
    'Responsible Party': 'Landlord',
    'Owner Responsibility': [
      'Release, indemnify, and hold harmless the Tenant',
      'Cover any and all demands, expenses, fees, costs'
    ],
    Reasoning: [
      'Hazardous Materials introduced by the Landlord'
    ],
    Citation: [
      {
        docId: 'Commercial Lease Agreement.pdf',
        pageNumbers: [13],
        section: ['Section (d)']
      }
    ]
  };
  
  try {
    const snippet = transformObligationToSnippet(mockObligation, 0);
    
    console.log('✅ Transformation successful!');
    console.log(`   Snippet ID: ${snippet.id}`);
    console.log(`   Title: ${snippet.title}`);
    console.log(`   Document ID: ${snippet.documentId}`);
    console.log(`   Page: ${snippet.pdfReference.page}`);
    console.log(`   Responsible Party: ${snippet.fieldMappings.responsibleParty}`);
    console.log(`   Owner Responsibility: ${snippet.fieldMappings.maintenanceOwnerResponsibility.substring(0, 50)}...`);
    console.log(`   Reasoning: ${snippet.fieldMappings.maintenanceReasoning}`);
    
    return true;
  } catch (error) {
    console.error('❌ Transformation failed:', error);
    return false;
  }
}

/**
 * Run all tests
 */
export async function runAllTests() {
  console.log('\n🚀 Starting API Service Tests\n');
  console.log('='.repeat(50));
  
  const results = {
    query: false,
    transform: false,
  };
  
  // Test 1: Query Obligations
  results.query = await testQueryObligations();
  console.log('='.repeat(50));
  
  // Test 2: Transform Obligation
  results.transform = testTransformObligation();
  console.log('='.repeat(50));
  
  // Summary
  console.log('\n📊 Test Summary:');
  console.log(`   Query Obligations: ${results.query ? '✅ PASS' : '❌ FAIL'}`);
  console.log(`   Transform Obligation: ${results.transform ? '✅ PASS' : '❌ FAIL'}`);
  
  const passCount = Object.values(results).filter(r => r).length;
  const totalCount = Object.keys(results).length;
  
  console.log(`\n   Total: ${passCount}/${totalCount} tests passed`);
  
  if (passCount === totalCount) {
    console.log('\n🎉 All tests passed! Backend integration is working correctly.\n');
  } else {
    console.log('\n⚠️  Some tests failed. Check the backend server and configuration.\n');
  }
}

// Export for use in browser console
if (typeof window !== 'undefined') {
  (window as any).apiTests = {
    runAll: runAllTests,
    testQuery: testQueryObligations,
    testTransform: testTransformObligation,
  };
  
  console.log('💡 API tests loaded! Run tests from console:');
  console.log('   - window.apiTests.runAll()');
  console.log('   - window.apiTests.testQuery()');
  console.log('   - window.apiTests.testTransform()');
}
