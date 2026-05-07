"""
Auto-generate keywords for individual obligations at the responsibility line level.
This replaces the static related_keywords approach with dynamic keyword generation.
"""

import re
import logging
from typing import Any, Dict, List, Set
from collections import Counter

logger = logging.getLogger(__name__)

# Domain-specific terms for legal obligations
LEGAL_DOMAIN_TERMS = {
    # Financial terms
    'financial': ['rent', 'payment', 'cost', 'expense', 'fee', 'charge', 'deposit', 'reimbursement', 
                  'operating expenses', 'base rent', 'additional rent', 'cam', 'triple net', 'holdover', 
                  'abatement', 'real estate taxes', 'assessed', 'levied', 'installment', 'monthly'],
    
    # Maintenance terms
    'maintenance': ['maintain', 'repair', 'replace', 'service', 'upkeep', 'restoration', 'hvac', 
                    'plumbing', 'electrical', 'mechanical', 'heating', 'cooling', 'ventilation', 
                    'sprinkler', 'fixture', 'alteration', 'improvement'],
    
    # Utility terms
    'utilities': ['utility', 'utilities', 'water', 'gas', 'heat', 'light', 'electricity', 'telephone', 
                  'sewer', 'meter', 'service'],
    
    # Legal/compliance terms
    'legal': ['comply', 'compliance', 'default', 'violation', 'law', 'regulation', 'permit', 'license', 
              'insurance', 'indemnify', 'liability', 'notice', 'consent', 'approval'],
    
    # Property terms
    'property': ['premises', 'building', 'structure', 'roof', 'exterior', 'interior', 'common area', 
                 'tenant improvement', 'landlord service', 'occupancy'],
    
    # Time/duration terms
    'temporal': ['term', 'expiration', 'effective date', 'monthly', 'annual', 'daily', 'duration', 
                 'period', 'deadline', 'within', 'days', 'year-over-year']
}

# Stop words to exclude from keyword generation
STOP_WORDS = {
    'a', 'an', 'and', 'are', 'as', 'at', 'be', 'by', 'for', 'from', 'has', 'he', 'in', 'is', 
    'it', 'its', 'of', 'on', 'that', 'the', 'to', 'was', 'will', 'with', 'or', 'but', 'not', 
    'this', 'these', 'they', 'their', 'them', 'than', 'then', 'there', 'when', 'where', 'who', 
    'which', 'what', 'how', 'why', 'all', 'any', 'each', 'few', 'more', 'most', 'other', 'some', 
    'such', 'no', 'nor', 'only', 'own', 'same', 'so', 'can', 'could', 'should', 'would'
}

def extract_key_phrases(text: str) -> List[str]:
    """
    Extract meaningful phrases from text using patterns and domain knowledge.
    """
    if not text or not isinstance(text, str):
        return []
    
    text_lower = text.lower()
    phrases = []
    
    # Extract multi-word domain-specific phrases first
    domain_patterns = [
        r'base rent', r'additional rent', r'operating expenses?', r'real estate taxes?',
        r'tenant improvement', r'landlord[\'s]? service', r'common area',
        r'triple net', r'year-over-year', r'effective date', r'expiration date',
        r'hvac system', r'sprinkler system', r'electrical system', r'plumbing system',
        r'mechanical system', r'heating system', r'cooling system', r'ventilation system',
        r'utility service', r'telephone service', r'building maintenance',
        r'structural repair', r'interior maintenance', r'exterior maintenance'
    ]
    
    for pattern in domain_patterns:
        matches = re.finditer(pattern, text_lower)
        for match in matches:
            phrases.append(match.group())
    
    # Extract other meaningful phrases (2-4 words)
    # Look for noun phrases, verb phrases with objects
    phrase_patterns = [
        r'\b(?:pay|send|receive|maintain|repair|replace|provide|ensure|comply|install)\s+[\w\s]{1,30}?\b',
        r'\b[\w]+\s+(?:expenses?|costs?|fees?|charges?|payments?|taxes?|maintenance|repairs?|services?)\b',
        r'\b(?:monthly|annual|daily)\s+[\w\s]{1,20}?\b'
    ]
    
    for pattern in phrase_patterns:
        matches = re.finditer(pattern, text_lower)
        for match in matches:
            phrase = match.group().strip()
            if len(phrase.split()) >= 2 and len(phrase.split()) <= 4:
                phrases.append(phrase)
    
    return list(set(phrases))  # Remove duplicates


def extract_keywords_from_text(text: str) -> List[str]:
    """
    Extract keywords from a single text string.
    Returns a list of relevant keywords and phrases.
    """
    if not text or not isinstance(text, str):
        return []
    
    keywords = []
    text_lower = text.lower()
    
    # First, extract key phrases
    phrases = extract_key_phrases(text)
    keywords.extend(phrases)
    
    # Extract individual domain-relevant terms
    words = re.findall(r'\b\w+\b', text_lower)
    
    # Score words based on domain relevance
    word_scores = Counter()
    
    for word in words:
        if len(word) <= 2 or word in STOP_WORDS:
            continue
            
        # Check against domain terms
        for category, terms in LEGAL_DOMAIN_TERMS.items():
            for term in terms:
                if word == term or (len(word) > 3 and word in term) or (len(term) > 3 and term in word):
                    word_scores[word] += 3  # High score for exact domain matches
                    break
        
        # Basic scoring for other terms
        if word not in word_scores:
            if len(word) > 8:  # Long words often more specific
                word_scores[word] += 2
            elif len(word) > 4:
                word_scores[word] += 1
    
    # Add top scoring individual words
    top_words = [word for word, score in word_scores.most_common(10) if score > 0]
    keywords.extend(top_words)
    
    # Remove duplicates while preserving order
    seen = set()
    unique_keywords = []
    for kw in keywords:
        kw_clean = kw.strip()
        if kw_clean and kw_clean not in seen:
            seen.add(kw_clean)
            unique_keywords.append(kw_clean)
    
    return unique_keywords[:15]  # Limit to top 15 keywords


def generate_obligation_keywords(obligation: Dict[str, Any]) -> List[str]:
    """
    Generate keywords for a single obligation by analyzing all its responsibility lines.
    
    Args:
        obligation: Dictionary containing obligation data with 'Owner Responsibility' field
        
    Returns:
        List of auto-generated keywords for this obligation
    """
    if not isinstance(obligation, dict):
        return []
    
    # Get all responsibility text
    responsibility_data = obligation.get("Owner Responsibility", [])
    
    if not responsibility_data:
        return []
    
    # Convert to list of strings
    responsibility_lines = []
    if isinstance(responsibility_data, str):
        responsibility_lines = [responsibility_data]
    elif isinstance(responsibility_data, list):
        responsibility_lines = [str(item).strip() for item in responsibility_data if item]
    else:
        responsibility_lines = [str(responsibility_data)]
    
    # Generate keywords from each responsibility line
    all_keywords = []
    
    for line in responsibility_lines:
        if not line or not line.strip():
            continue
            
        line_keywords = extract_keywords_from_text(line)
        all_keywords.extend(line_keywords)
    
    # Also include DutyType if available
    duty_type = obligation.get("DutyType", "")
    if duty_type:
        duty_keywords = extract_keywords_from_text(str(duty_type))
        all_keywords.extend(duty_keywords)
    
    # Also include Responsible Party info
    party = obligation.get("Responsible Party", "")
    if party:
        party_lower = str(party).lower().strip()
        if party_lower in ['tenant', 'landlord', 'lessee', 'lessor']:
            all_keywords.append(party_lower)
    
    # Remove duplicates and limit
    seen = set()
    final_keywords = []
    for kw in all_keywords:
        kw_clean = kw.strip().lower()
        if kw_clean and kw_clean not in seen and len(kw_clean) > 1:
            seen.add(kw_clean)
            final_keywords.append(kw)
    
    return final_keywords[:20]  # Limit to top 20 keywords


def enhance_obligation_with_auto_keywords(obligation: Dict[str, Any]) -> Dict[str, Any]:
    """
    Add auto-generated keywords to an obligation, replacing or supplementing existing related_keywords.
    
    Args:
        obligation: Original obligation dictionary
        
    Returns:
        Enhanced obligation with auto_generated_keywords field
    """
    if not isinstance(obligation, dict):
        return obligation
    
    enhanced = obligation.copy()
    
    # Generate new keywords
    auto_keywords = generate_obligation_keywords(obligation)
    
    if auto_keywords:
        enhanced["auto_generated_keywords"] = auto_keywords
        logger.debug(f"Generated {len(auto_keywords)} keywords for obligation: {auto_keywords[:5]}...")
    
    return enhanced


def process_consolidated_results_with_auto_keywords(consolidated_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Process consolidated results and add auto-generated keywords to all obligations.
    
    Args:
        consolidated_results: List of category dictionaries from consolidated JSON
        
    Returns:
        Enhanced results with auto-generated keywords
    """
    if not consolidated_results or not isinstance(consolidated_results, list):
        return consolidated_results
    
    enhanced_results = []
    total_obligations = 0
    
    for category_data in consolidated_results:
        if not isinstance(category_data, dict):
            enhanced_results.append(category_data)
            continue
        
        enhanced_category = category_data.copy()
        obligations = category_data.get("obligations", [])
        
        if not obligations:
            enhanced_results.append(enhanced_category)
            continue
        
        enhanced_obligations = []
        for obligation in obligations:
            enhanced_obligation = enhance_obligation_with_auto_keywords(obligation)
            enhanced_obligations.append(enhanced_obligation)
            total_obligations += 1
        
        enhanced_category["obligations"] = enhanced_obligations
        enhanced_results.append(enhanced_category)
    
    logger.info(f"Enhanced {total_obligations} obligations with auto-generated keywords")
    return enhanced_results


def flatten_obligations_for_individual_indexing(consolidated_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Flatten consolidated results into individual obligations for obligation-level indexing.
    Each obligation becomes a separate indexable unit with its own auto-generated keywords.
    
    Args:
        consolidated_results: List of category dictionaries from consolidated JSON
        
    Returns:
        List of individual obligations with metadata about their source category
    """
    if not consolidated_results or not isinstance(consolidated_results, list):
        return []
    
    individual_obligations = []
    
    for category_data in consolidated_results:
        if not isinstance(category_data, dict):
            continue
            
        category_name = category_data.get("category", "Unknown Category")
        obligations = category_data.get("obligations", [])
        
        for i, obligation in enumerate(obligations):
            if not isinstance(obligation, dict):
                continue
            
            # Create enhanced individual obligation
            enhanced_obligation = enhance_obligation_with_auto_keywords(obligation)
            
            # Add category metadata
            enhanced_obligation["source_category"] = category_name
            enhanced_obligation["obligation_index_in_category"] = i
            
            individual_obligations.append(enhanced_obligation)
    
    logger.info(f"Flattened {len(individual_obligations)} individual obligations from {len(consolidated_results)} categories")
    return individual_obligations