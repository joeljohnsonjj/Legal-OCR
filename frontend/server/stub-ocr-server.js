const http = require('http');

const PORT = 8000;

// Stub response matching BackendQueryResponse from apiService.ts
const stubObligations = [
    {
        DutyType: 'Base Rent Payment',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Pay monthly Base Rent stated in Section 1 in advance on or before the first day of each month during the Term.',
            'Pay all Base Rent free and clear of any impositions, taxes, liens, charges, or expenses.'
        ],
        Reasoning: [
            'Obligation to pay rent as specified in the lease agreement.'
        ],
        Citation: [
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [1],
                section: ['d. Base Rent']
            },
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [3],
                section: ['RENT']
            },
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [4],
                section: ['Triple Net Lease']
            }
        ]
    },
    {
        DutyType: 'Rent Payment',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Pay all Rents and other payments until the date of termination due to condemnation.',
            'Equitably reduce Rent based on the proportion by which the floor area of any structures is reduced if a portion is taken by the condemning authority.'
        ],
        Reasoning: [
            'Obligation to pay rent until termination of lease due to condemnation.',
            'Reduction in rent is based on the loss of usable space.'
        ],
        Citation: [
            {
                docId: 'Commercial Lease Agreement - Buyer Triple Net.pdf',
                pageNumbers: [11],
                section: ['Clause discussing condemnation']
            }
        ]
    },
    {
        DutyType: 'Rent Payment',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Pay any sum, including Rent, due under this Lease.'
        ],
        Reasoning: [
            'Failure to pay Rent constitutes an Event of Default.'
        ],
        Citation: [
            {
                docId: 'Commercial Lease Agreement - Buyer Triple Net.pdf',
                pageNumbers: [14],
                section: ['DEFAULT']
            }
        ]
    },
    {
        DutyType: 'Liability Insurance Payment',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Pay for and maintain commercial general liability insurance with a combined single limit of not less than $2,000,000 per occurrence.',
            'Maintain a deductible of not more than $10,000.'
        ],
        Reasoning: [
            'To cover potential liabilities for personal injury or property damage.',
            'Ensures financial protection for both Tenant and Landlord.'
        ],
        Citation: [
            {
                docId: 'Commercial Lease Agreement - Buyer Triple Net.pdf',
                pageNumbers: [11],
                section: ['Insurance']
            }
        ]
    },
    {
        DutyType: 'Operating Costs Payment',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Pay one-twelfth of Tenant\'s Pro Rata Share of Operating Costs on the first day of each month with payment of Base Rent.',
            'Pay any shortfall within 30 days following receipt of the Operating Costs Statement if the Pro Rata Share exceeds the sum of the monthly installments.'
        ],
        Reasoning: [
            'Tenant is responsible for a share of the operating costs as part of the lease agreement.',
            'Ensures Landlord recovers costs incurred for the operation and maintenance of the property.'
        ],
        Citation: [
            {
                docId: 'Commercial Lease Agreement - Buyer Triple Net.pdf',
                pageNumbers: [6],
                section: ['Paragraph discussing Operating Costs']
            }
        ]
    },
    {
        DutyType: 'Taxes Payment',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Pay all taxes, assessments, liens, and license fees levied or imposed related to Tenant\'s use of the Premises',
            'Pay all Taxes on Tenant\'s personal property located on the Premises'
        ],
        Reasoning: [
            'Obligation arises from Tenant\'s use of the Premises',
            'Ensures compliance with local tax laws'
        ],
        Citation: [
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [8],
                section: ['10. TAXES AND ASSESSMENTS']
            }
        ]
    },
    {
        DutyType: 'Security Deposit',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Deliver the security deposit specified in Section 1 upon execution of the Lease.',
            'Replenish the security deposit within five days after written demand if it becomes insufficient.'
        ],
        Reasoning: [
            'The security deposit serves as a financial guarantee for Tenant\'s obligations under the Lease.'
        ],
        Citation: [
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [1],
                section: ['f. Security Deposit']
            },
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [4],
                section: ['SECURITY DEPOSIT']
            }
        ]
    },
    {
        DutyType: 'Indemnification Payment',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Defend, indemnify, and hold Landlord harmless against all liabilities, damages, costs, and expenses, including attorneys\' fees.'
        ],
        Reasoning: [
            'Tenant is liable for costs arising from negligent acts or breaches of the Lease.'
        ],
        Citation: [
            {
                docId: 'Commercial Lease Agreement - Buyer Triple Net.pdf',
                pageNumbers: [12],
                section: ['Indemnification']
            }
        ]
    },
    {
        DutyType: 'Indemnification Payment',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Indemnify, defend and hold Landlord harmless from any claims, judgments, damages, penalties, fines, costs, liabilities or losses incurred by Landlord due to Tenant\'s breach of obligations regarding Hazardous Material.'
        ],
        Reasoning: [
            'Tenant is responsible for any contamination caused by its actions.'
        ],
        Citation: [
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [18],
                section: ['HAZARDOUS MATERIAL']
            }
        ]
    },
    {
        DutyType: 'Indemnification Payment',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Indemnify and hold harmless Landlord against any loss, cost, liability or expense incurred by Landlord as a result of any claim asserted by any broker, finder or other person.'
        ],
        Reasoning: [
            'Tenant is responsible for any claims related to brokers not disclosed in the lease.'
        ],
        Citation: [
            {
                docId: 'Commercial Lease Agreement - Buyer Triple Net.pdf',
                pageNumbers: [19],
                section: ['Brokers\' Fees']
            }
        ]
    },
    {
        DutyType: 'Reimbursement of Costs',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Reimburse Landlord for any costs regarding the operation, maintenance, and repair of the Premises, the Building, or the Property paid directly by Tenant or other tenants.'
        ],
        Reasoning: [
            'Tenant must reimburse Landlord for costs incurred that are not part of operating cost recoveries under other tenant leases.'
        ],
        Citation: [
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [6],
                section: ['Paragraph discussing reimbursement of costs']
            }
        ]
    },
    {
        DutyType: 'Reimbursement for Expenses',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Reimburse Landlord for expenses incurred in making payments or performing acts on Tenant\'s behalf within 10 days of demand.'
        ],
        Reasoning: [
            'To ensure Landlord is compensated for costs incurred due to Tenant\'s failure to perform.'
        ],
        Citation: [
            {
                docId: 'Commercial Lease Agreement - Buyer Triple Net.pdf',
                pageNumbers: [17],
                section: ['RIGHT TO PERFORM']
            }
        ]
    },
    {
        DutyType: 'Operating Costs Reimbursement',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Reimburse Landlord for partial costs of taxes and assessments included in Operating Costs'
        ],
        Reasoning: [
            'Tenant is responsible for a portion of the costs incurred by Landlord related to property taxes'
        ],
        Citation: [
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [8],
                section: ['10. TAXES AND ASSESSMENTS']
            }
        ]
    },
    {
        DutyType: 'Property Insurance Payment',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Pay for and maintain special form clauses of loss coverage property insurance for all of Tenant\'s property.'
        ],
        Reasoning: [
            'To protect Tenant\'s property against loss or damage.'
        ],
        Citation: [
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [11],
                section: ['Insurance']
            }
        ]
    },
    {
        DutyType: 'Indemnification for Liens',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Indemnify, defend, and hold Landlord harmless from liability for any liens created by or through Tenant.'
        ],
        Reasoning: [
            'Tenant is responsible for any liens arising from their actions and must protect Landlord from related liabilities.'
        ],
        Citation: [
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [13],
                section: ['LIENS']
            }
        ]
    },
    {
        DutyType: 'Liability for Damages',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Be liable for any and all damages or expenses incurred by Landlord as a result of Tenant\'s holdover.'
        ],
        Reasoning: [
            'Tenant\'s holdover may cause Landlord to incur additional costs.'
        ],
        Citation: [
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [16],
                section: ['HOLDOVER']
            }
        ]
    },
    {
        DutyType: 'Indebtedness Payment',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Pay any indebtedness to Landlord other than rent from the proceeds of any reletting.'
        ],
        Reasoning: [
            'Tenant\'s obligation to pay any debts to Landlord is prioritized from reletting proceeds.'
        ],
        Citation: [
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [15],
                section: ['Re-Entry and Reletting']
            }
        ]
    },
    {
        DutyType: 'Reimbursement for Improvements',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Promptly reimburse Landlord for any work performed by Landlord that is not part of Landlord\'s Work.'
        ],
        Reasoning: [
            'Tenant is responsible for costs incurred by Landlord for improvements not covered under Landlord\'s obligations.'
        ],
        Citation: [
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [27],
                section: ['Exhibit C, Paragraph discussing improvements']
            }
        ]
    },
    {
        DutyType: 'Pro Rata Share Adjustment',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Tenant\'s Base Rent and Pro Rata Share shall be proportionally adjusted in the event of any adjustment to the Premises\', Building\'s or Property\'s rentable floor area.'
        ],
        Reasoning: [
            'Adjustment is necessary to reflect changes in the rentable area.'
        ],
        Citation: [
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [2],
                section: ['Tenant\'s Pro Rata Share']
            }
        ]
    },
    {
        DutyType: 'Completion of Tenant\'s Work',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Complete Tenant\'s Work at its sole cost and expense.'
        ],
        Reasoning: [
            'Tenant is obligated to complete the work identified in the Tenant Improvement Plans at its own expense.'
        ],
        Citation: [
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [28],
                section: ['Improvements to be Completed by Tenant']
            }
        ]
    },
    {
        DutyType: 'Additional Rent for HVAC Services',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Pay for HVAC services provided outside of regular hours at an hourly rate established by Landlord.'
        ],
        Reasoning: [
            'Tenant incurs additional costs for HVAC services beyond standard provisions.'
        ],
        Citation: [
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [7],
                section: ['UTILITIES AND SERVICES']
            }
        ]
    },
    {
        DutyType: 'Utility Payment',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Pay for all utilities separately metered to the Premises.',
            'Pay for all other utilities and services required by Tenant, except those provided by Landlord.'
        ],
        Reasoning: [
            'Tenant is responsible for all utility costs not covered by Landlord.'
        ],
        Citation: [
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [7],
                section: ['UTILITIES AND SERVICES']
            }
        ]
    },
    {
        DutyType: 'Reletting Expenses',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Pay all Reletting Expenses incurred by Landlord in connection with reletting the Premises.'
        ],
        Reasoning: [
            'Tenant is responsible for costs associated with reletting due to their default.'
        ],
        Citation: [
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [15],
                section: ['Re-Entry and Reletting']
            }
        ]
    },
    {
        DutyType: 'Tenant Improvements Payment',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Tenant shall be responsible for performing any work necessary to bring the Premises into a condition satisfactory to Tenant.',
            'Tenant acknowledges responsibility for making any corrections, alterations and repairs to the Premises (other than Landlord\'s Work).'
        ],
        Reasoning: [
            'Tenant is responsible for costs associated with necessary work on the Premises.'
        ],
        Citation: [
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [2],
                section: ['Acceptance of Premises']
            }
        ]
    },
    {
        DutyType: 'Damages for Rent',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Remain liable for damages equal to Rent and other sums that would have been owing under the Lease for the balance of the Term upon termination.',
            'Pay damages monthly on the days rent or other amounts would have been payable under the Lease.'
        ],
        Reasoning: [
            'Liability exists due to Tenant\'s failure to perform obligations under the Lease.',
            'Landlord is entitled to collect damages as specified in the event of Tenant\'s default.'
        ],
        Citation: [
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [15],
                section: ['Remedies']
            }
        ]
    },
    {
        DutyType: 'Holdover Rent Payment',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Pay 150% of the last rental rate under this Lease if Tenant holds over without consent.'
        ],
        Reasoning: [
            'Tenant is liable for increased rent during holdover tenancy.'
        ],
        Citation: [
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [16],
                section: ['HOLDOVER']
            }
        ]
    },
    {
        DutyType: 'Cost Payment for Tenant\'s Work',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Pay all costs, expenses, and fees associated with completing the Tenant\'s Work in accordance with the Tenant Improvement Plans.'
        ],
        Reasoning: [
            'Tenant is fully responsible for the financial obligations related to their own improvements.'
        ],
        Citation: [
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [30],
                section: ['Improvement Allowance']
            }
        ]
    },
    {
        DutyType: 'Operating Costs Adjustment',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Pay Tenant\'s Pro Rata Share of Operating Costs based on adjusted Operating Costs if the Building is not 90% occupied.'
        ],
        Reasoning: [
            'Tenant\'s share of Operating Costs is adjusted based on occupancy rates.'
        ],
        Citation: [
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [5],
                section: ['OPERATING COSTS']
            }
        ]
    },
    {
        DutyType: 'Storage Cost Payment',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Pay for the storage of property if Tenant fails to remove it at Landlord\'s request.'
        ],
        Reasoning: [
            'Tenant incurs costs for storage if they do not comply with Landlord\'s request to remove property.'
        ],
        Citation: [
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [16],
                section: ['Failure to Remove Property']
            }
        ]
    },
    {
        DutyType: 'Alterations Cost Limit',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Obtain Landlord\'s consent for Alterations not exceeding $10,000'
        ],
        Reasoning: [
            'Tenant must seek approval for any alterations that may incur costs, ensuring Landlord\'s oversight'
        ],
        Citation: [
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [8],
                section: ['12. ALTERATIONS']
            }
        ]
    },
    {
        DutyType: 'Additional Rent Payment',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Pay any other additional payments, including Operating Costs, due to Landlord.',
            'Pay all Additional Rent and other impositions, insurance premiums, repair and maintenance charges, and any other charges.'
        ],
        Reasoning: [
            'Additional payments are required under the lease agreement.'
        ],
        Citation: [
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [3],
                section: ['RENT']
            },
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [4],
                section: ['Triple Net Lease']
            },
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [16],
                section: ['Nonpayment of Additional Rent']
            }
        ]
    },
    {
        DutyType: 'Excess Cost Payment',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Pay any costs associated with completing Tenant\'s Work that exceed the Improvement Allowance.'
        ],
        Reasoning: [
            'Tenant is responsible for any costs beyond the provided Improvement Allowance.'
        ],
        Citation: [
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [30],
                section: ['Improvement Allowance']
            }
        ]
    },
    {
        DutyType: 'Attorney Fees Payment',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Pay a reasonable sum for attorneys\' fees if Tenant engages an attorney to collect monies due or bring any action against Landlord.'
        ],
        Reasoning: [
            'To cover legal costs incurred in actions arising out of the Lease.'
        ],
        Citation: [
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [17],
                section: ['COSTS AND ATTORNEYS\' FEES']
            }
        ]
    },
    {
        DutyType: 'Cost of Changes to Improvement Plans',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Bear any costs and expenses resulting from changes to Landlord\'s Work that exceed the Improvement Allowance.'
        ],
        Reasoning: [
            'Tenant is responsible for costs exceeding the Improvement Allowance as per the agreement.'
        ],
        Citation: [
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [28],
                section: ['Clause discussing costs and expenses related to Landlord\'s Work']
            }
        ]
    },
    {
        DutyType: 'Cost of Remediation',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Take all actions necessary to return the Premises or adjacent property to the condition existing prior to the release of any Hazardous Material at Tenant\'s sole expense.'
        ],
        Reasoning: [
            'Tenant must cover costs associated with remediation of any Hazardous Material it brings onto the property.'
        ],
        Citation: [
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [18],
                section: ['HAZARDOUS MATERIAL']
            }
        ]
    },
    {
        DutyType: 'Additional Utility Charges',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Pay reasonable additional charges for utility service charges above usual and customary levels.'
        ],
        Reasoning: [
            'Landlord reserves the right to charge Tenant for excessive utility usage.'
        ],
        Citation: [
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [7],
                section: ['UTILITIES AND SERVICES']
            }
        ]
    },
    {
        DutyType: 'Late Charge Payment',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Pay an amount equal to the greater of $100 or 5% of the delinquent amount if payments are not received within five business days after their due date.'
        ],
        Reasoning: [
            'This provision imposes a financial penalty for late payments to incentivize timely payment.'
        ],
        Citation: [
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [4],
                section: ['Late Charges; Default Interest']
            }
        ]
    },
    {
        DutyType: 'Additional Rent for Repairs',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Pay the cost of repairs made by Landlord if Tenant fails to perform its obligations.'
        ],
        Reasoning: [
            'To cover costs incurred by Landlord for repairs due to Tenant\'s failure to maintain the Premises.'
        ],
        Citation: [
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [9],
                section: ['REPAIRS AND MAINTENANCE; SURRENDER']
            }
        ]
    },
    {
        DutyType: 'Signage Installation Cost',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Install and maintain any approved signage at Tenant\'s sole expense',
            'Remove signage at Tenant\'s expense upon expiration or termination of the Lease',
            'Repair any injury or damage to the Premises caused by installation or removal of signage'
        ],
        Reasoning: [
            'Tenant is responsible for all costs associated with signage installation and maintenance',
            'Tenant must cover costs for removal of signage and any damages caused'
        ],
        Citation: [
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [10],
                section: ['SIGNAGE']
            }
        ]
    },
    {
        DutyType: 'Repair Costs for Tenant\'s Work',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Repair any damage to the Premises caused by removal of improvements performed as part of Tenant\'s Work.'
        ],
        Reasoning: [
            'To restore the Premises to its original condition after alterations.'
        ],
        Citation: [
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [9],
                section: ['Paragraph discussing repairs and maintenance']
            }
        ]
    },
    {
        DutyType: 'Removal Cost Payment',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Remove Tenant\'s Work at its sole cost and expense upon expiration or earlier termination of the Lease Term.'
        ],
        Reasoning: [
            'Tenant is financially responsible for the removal of their improvements.'
        ],
        Citation: [
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [30],
                section: ['Removal of Improvements/Surrender']
            }
        ]
    },
    {
        DutyType: 'Maintenance Costs',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Maintain the entire Premises in good condition.',
            'Make all non-structural repairs and replacements necessary to keep the Premises safe.'
        ],
        Reasoning: [
            'To ensure the Premises are safe and in good condition.'
        ],
        Citation: [
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [9],
                section: ['REPAIRS AND MAINTENANCE; SURRENDER']
            }
        ]
    },
    {
        DutyType: 'Lien Removal Cost',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Remove any lien filed against the Premises by any person claiming by, through or under Tenant, at Tenant\'s expense.'
        ],
        Reasoning: [
            'Tenant must bear the cost of removing any liens they create, ensuring the property remains free from encumbrances.'
        ],
        Citation: [
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [13],
                section: ['LIENS']
            }
        ]
    },
    {
        DutyType: 'Late Fee Payment',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Pay late fees and interest on any overdue payments.'
        ],
        Reasoning: [
            'Late fees and interest are considered Additional Rent, and failure to pay allows Landlord to exercise rights and remedies.'
        ],
        Citation: [
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [3],
                section: ['RENT']
            }
        ]
    },
    {
        DutyType: 'Construction Cost',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Prepare and submit a preliminary sketch of the Tenant Improvements for Landlord\'s review.',
            'Prepare construction documents for Landlord\'s review and approval.',
            'Submit Tenant Improvements Plans to the appropriate governmental body for plan checking and issuance of necessary permits.'
        ],
        Reasoning: [
            'Tenant is responsible for the costs associated with preparing and submitting plans and documents necessary for construction.',
            'Tenant must incur costs to obtain necessary permits and approvals for Tenant\'s Work.'
        ],
        Citation: [
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [29],
                section: ['General Requirements']
            }
        ]
    },
    {
        DutyType: 'Transfer Processing Cost',
        'Responsible Party': 'Tenant',
        'Owner Responsibility': [
            'Pay the reasonable cost of processing a Transfer request, including attorneys\' fees, upon demand of Landlord, up to a maximum of $1,250.'
        ],
        Reasoning: [
            'Tenant is required to cover costs associated with processing Transfer requests as stipulated in the lease.'
        ],
        Citation: [
            {
                docId: 'MTNNN.pdf',
                pageNumbers: [13],
                section: ['ASSIGNMENT AND SUBLETTING']
            }
        ]
    }
];

const server = http.createServer((req, res) => {
  // CORS for local dev
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type');

  if (req.method === 'OPTIONS') {
    res.writeHead(204);
    res.end();
    return;
  }

  // Streaming endpoint: /query/stream (NDJSON format)
  if (req.method === 'POST' && req.url === '/query/stream') {
    let body = '';
    req.on('data', chunk => { body += chunk; });
    req.on('end', () => {
      try {
        const parsed = body ? JSON.parse(body) : {};
        const query = parsed.query || '';
        const documentIds = parsed.document_ids || [];

        // Set headers for NDJSON streaming
        res.setHeader('Content-Type', 'application/x-ndjson');
        res.setHeader('Transfer-Encoding', 'chunked');
        res.writeHead(200);

        let obligationIndex = 0;
        const totalObligations = stubObligations.length;

        // Stream each obligation with a delay to simulate real streaming
        // 400ms delay (0.4 seconds) - realistic for LLM generation speed
        const streamInterval = setInterval(() => {
          if (obligationIndex < totalObligations) {
            const obligation = stubObligations[obligationIndex];
            
            // Send obligation as NDJSON line
            const obligationEvent = {
              type: 'obligation',
              data: obligation
            };
            res.write(JSON.stringify(obligationEvent) + '\n');
            
            obligationIndex++;
          } else {
            // All obligations sent, send metadata and close
            const metadataEvent = {
              type: 'metadata',
              data: {
                query,
                total_documents_searched: documentIds.length || 1,
                total_obligations_found: totalObligations,
                processed_at: new Date().toISOString()
              }
            };
            res.write(JSON.stringify(metadataEvent) + '\n');
            res.end();
            clearInterval(streamInterval);
          }
        }, 400); // Send one obligation every 400ms (0.4 seconds)

      } catch (e) {
        const errorEvent = {
          type: 'error',
          message: 'Invalid JSON body'
        };
        res.write(JSON.stringify(errorEvent) + '\n');
        res.end();
      }
    });
    return;
  }

  // Non-streaming endpoint: /query (returns all at once)
  if (req.method === 'POST' && req.url === '/query') {
    let body = '';
    req.on('data', chunk => { body += chunk; });
    req.on('end', () => {
      try {
        const parsed = body ? JSON.parse(body) : {};
        const query = parsed.query || '';
        const documentIds = parsed.document_ids || [];

        const response = {
          query,
          total_documents_searched: documentIds.length || 1,
          total_obligations_found: 2,
          results: stubObligations,
          processed_at: new Date().toISOString()
        };

        res.setHeader('Content-Type', 'application/json');
        res.writeHead(200);
        res.end(JSON.stringify(response));
      } catch (e) {
        res.writeHead(400);
        res.end(JSON.stringify({ error: 'Invalid JSON body' }));
      }
    });
    return;
  }

  res.writeHead(404);
  res.end('Not found');
});

server.listen(PORT, () => {
  console.log(`Stub OCR server at http://localhost:${PORT}`);
  console.log(`  - POST /query (non-streaming, returns all results at once)`);
  console.log(`  - POST /query/stream (streaming NDJSON, progressive results)`);
});
