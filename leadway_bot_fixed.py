"""
Leadway Health WhatsApp Bot - FIXED VERSION
============================================
Proper tool calling with correct message flow
"""

import os
import time
import json
import requests
from typing import Optional, Dict
from dotenv import load_dotenv
from langchain_anthropic import ChatAnthropic
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.messages import HumanMessage, AIMessage
from langchain_core.tools import tool

load_dotenv()

# ============================================================================
# LEADWAY API CLIENT
# ============================================================================

class LeadwayAPIClient:
    def __init__(self):
        self.base_url = os.getenv("LEADWAY_API_BASE_URL", "https://prognosis-api.leadwayhealth.com/api")
        self.username = os.getenv("LEADWAY_API_USERNAME")
        self.password = os.getenv("LEADWAY_API_PASSWORD")
        self.token = None
        self.token_expiry = 0
    
    def login(self) -> bool:
        try:
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "LeadwayHealthBot/1.0"
            }
            
            response = requests.post(
                f"{self.base_url}/ApiUsers/Login",
                json={"Username": self.username, "Password": self.password},
                headers=headers,
                timeout=10
            )
            
            if response.status_code == 200:
                data = response.json()
                self.token = (
                    data.get("token") or 
                    data.get("Token") or 
                    data.get("access_token") or
                    data.get("AccessToken")
                )
                
                if self.token:
                    self.token_expiry = time.time() + 3600
                    print("✓ Connected to Leadway API")
                    return True
                else:
                    print(f"ERROR: No token in response. Keys: {list(data.keys())}")
                    return False
            else:
                print(f"Login failed: {response.status_code} - {response.text}")
                return False
                
        except Exception as e:
            print(f"Login error: {e}")
            return False
    
    def ensure_authenticated(self):
        if not self.token or time.time() >= self.token_expiry:
            if not self.login():
                raise Exception("Failed to authenticate")
    
    def get(self, endpoint: str, params: Dict = None) -> Optional[Dict]:
        self.ensure_authenticated()
        
        try:
            headers = {
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "LeadwayHealthBot/1.0"
            }
            
            response = requests.get(
                f"{self.base_url}/{endpoint}",
                params=params,
                headers=headers,
                timeout=10
            )
            
            if response.status_code == 200:
                return response.json()
            else:
                print(f"API error: {response.status_code}")
                return None
                
        except Exception as e:
            print(f"Request error: {e}")
            return None

api_client = LeadwayAPIClient()

# ============================================================================
# TOOLS
# ============================================================================

@tool
def lookup_member_for_id(phone_number: str) -> dict:
    """
    Look up member by phone number to get their Member ID.
    
    Args:
        phone_number: Member's 11-digit phone number
    
    Returns:
        dict with found, enrollee_id, name
    """
    try:
        result = api_client.get(
            "EnrolleeProfile/GetEnrolleeBioDataByMobileNo",
            params={"mobileno": phone_number}
        )
        
        if not result:
            return {"found": False}
        
        # The API returns a structure with "status" and "result" array
        if isinstance(result, dict):
            if result.get("status") == 200 and result.get("result"):
                # Get first result from array
                member_data = result["result"][0] if isinstance(result["result"], list) else result["result"]
            else:
                member_data = result
        else:
            member_data = result
        
        # Extract using ACTUAL field names from API
        enrollee_id = member_data.get("Member_EnrolleeID")
        customer_name = member_data.get("Member_CustomerName")
        
        # If no customer name, build it from parts
        if not customer_name:
            first = member_data.get("Member_FirstName", "")
            surname = member_data.get("Member_Surname", "")
            other = member_data.get("Member_othernames", "")
            customer_name = f"{first} {surname} {other}".strip()
        
        return {
            "found": True,
            "enrollee_id": enrollee_id,
            "name": customer_name
        }
    except Exception as e:
        print(f"[ERROR] {e}")
        return {"found": False, "error": str(e)}

@tool
def lookup_member_by_email(email: str) -> dict:
    """Look up member by email"""
    result = api_client.get(
        "EnrolleeProfile/GetEnrolleeBioDataByEmail",
        params={"email": email}
    )
    
    if not result:
        return {"found": False}
    
    # Handle response structure
    if isinstance(result, dict):
        if result.get("status") == 200 and result.get("result"):
            member_data = result["result"][0] if isinstance(result["result"], list) else result["result"]
        else:
            member_data = result
    else:
        member_data = result
    
    # Extract using correct field names
    enrollee_id = member_data.get("Member_EnrolleeID")
    customer_name = member_data.get("Member_CustomerName")
    
    if not customer_name:
        first = member_data.get("Member_FirstName", "")
        surname = member_data.get("Member_Surname", "")
        other = member_data.get("Member_othernames", "")
        customer_name = f"{first} {surname} {other}".strip()
    
    return {
        "found": True,
        "enrollee_id": enrollee_id,
        "name": customer_name
    }

@tool
def get_dependants(enrollee_id: str) -> dict:
    """
    Get list of dependants for a principal member.
    
    Args:
        enrollee_id: Principal member's enrollee ID (e.g., 21000645/0)
    
    Returns:
        dict with found and list of dependants
    """
    result = api_client.get(
        "EnrolleeProfile/GetEnrolleeDependantsByEnrolleeID",
        params={"enrolleeid": enrollee_id}
    )
    
    if not result:
        return {"found": False, "dependants": []}
    
    # Handle response structure
    if isinstance(result, dict):
        if result.get("status") == 200 and result.get("result"):
            dependants_data = result["result"]
        else:
            dependants_data = result if isinstance(result, list) else [result]
    else:
        dependants_data = result if isinstance(result, list) else []
    
    if not dependants_data:
        return {"found": False, "dependants": []}
    
    # Extract dependant information
    dependants_list = []
    for dep in dependants_data:
        enrollee_id = dep.get("Member_EnrolleeID") or dep.get("EnrolleeID")
        customer_name = dep.get("Member_CustomerName")
        
        if not customer_name:
            first = dep.get("Member_FirstName", "")
            surname = dep.get("Member_Surname", "")
            other = dep.get("Member_othernames", "")
            customer_name = f"{first} {surname} {other}".strip()
        
        relationship = dep.get("Member_Relationship") or dep.get("Relationship") or "Dependant"
        
        if customer_name and enrollee_id:
            dependants_list.append({
                "name": customer_name,
                "enrollee_id": enrollee_id,
                "relationship": relationship
            })
    
    return {
        "found": True,
        "count": len(dependants_list),
        "dependants": dependants_list
    }

@tool
def check_benefits(enrollee_id: str, benefit_type: str = "all") -> dict:
    """
    Check benefit limits for a member.
    
    Args:
        enrollee_id: Member's enrollee ID (e.g., 21000645/0)
        benefit_type: Type of benefit - "lens", "dental", "chronic", "surgery", "major_disease", "all"
    
    Returns:
        dict with benefit information showing limit, used, and balance
    """
    # Extract CIF number from enrollee ID (remove /0, /1 suffix)
    cif_number = enrollee_id.split('/')[0] if '/' in enrollee_id else enrollee_id
    
    print(f"[BENEFITS] Looking up benefits for CIF: {cif_number}, Type: {benefit_type}")
    
    benefit_endpoints = {
        "lens": "EnrolleeProfile/GetEnrolleeBenefitsByCif_LensFrames",
        "dental": "EnrolleeProfile/GetEnrolleeBenefitsByCif_Dental",
        "chronic": "EnrolleeProfile/GetEnrolleeBenefitsByCif_ChronicMedicines",
        "surgery": "EnrolleeProfile/GetEnrolleeBenefitsByCif_Surgery",
        "major_disease": "EnrolleeProfile/GetEnrolleeBenefitsByCif_MajorDisease",
        "all": "EnrolleeProfile/GetEnrolleeBenefitsByCif"
    }
    
    endpoint = benefit_endpoints.get(benefit_type.lower(), benefit_endpoints["all"])
    
    result = api_client.get(endpoint, params={"cifno": cif_number})
    
    # DEBUG: Show raw response
    print(f"[BENEFITS DEBUG] RAW API RESPONSE:")
    print(json.dumps(result, indent=2))
    
    if not result:
        return {"found": False, "message": "Unable to retrieve benefit information"}
    
    # Handle response structure
    benefits_data = None
    if isinstance(result, dict):
        if result.get("status") == 200 and result.get("result"):
            benefits_data = result["result"]
            print(f"[BENEFITS DEBUG] Extracted from status/result wrapper")
        else:
            benefits_data = result
            print(f"[BENEFITS DEBUG] Using direct result")
    
    if not benefits_data:
        return {"found": False, "message": "No benefit data available"}
    
    print(f"[BENEFITS DEBUG] Benefits data type: {type(benefits_data)}")
    print(f"[BENEFITS DEBUG] Benefits data: {benefits_data}")
    
    # Parse benefits
    benefits_list = []
    
    if isinstance(benefits_data, list):
        print(f"[BENEFITS DEBUG] Processing list of {len(benefits_data)} benefits")
        for item in benefits_data:
            print(f"[BENEFITS DEBUG] Item keys: {list(item.keys())}")
            benefit_name = item.get("BenefitName") or item.get("ServiceName") or "Benefit"
            limit = float(item.get("Limit") or item.get("BenefitLimit") or 0)
            used = float(item.get("Used") or item.get("AmountUsed") or 0)
            balance = float(item.get("Balance") or item.get("BalanceLeft") or (limit - used))
            
            print(f"[BENEFITS DEBUG] Parsed: {benefit_name} - Limit: {limit}, Used: {used}, Balance: {balance}")
            
            benefits_list.append({
                "name": benefit_name,
                "limit": limit,
                "used": used,
                "balance": balance
            })
    else:
        # Single benefit response
        print(f"[BENEFITS DEBUG] Processing single benefit, keys: {list(benefits_data.keys())}")
        benefit_name = benefits_data.get("BenefitName") or benefits_data.get("ServiceName") or benefit_type
        limit = float(benefits_data.get("Limit") or benefits_data.get("BenefitLimit") or 0)
        used = float(benefits_data.get("Used") or benefits_data.get("AmountUsed") or 0)
        balance = float(benefits_data.get("Balance") or benefits_data.get("BalanceLeft") or (limit - used))
        
        print(f"[BENEFITS DEBUG] Parsed: {benefit_name} - Limit: {limit}, Used: {used}, Balance: {balance}")
        
        benefits_list.append({
            "name": benefit_name,
            "limit": limit,
            "used": used,
            "balance": balance
        })
    
    return {
        "found": True,
        "benefit_type": benefit_type,
        "benefits": benefits_list
    }

TOOLS = [lookup_member_for_id, lookup_member_by_email, get_dependants, check_benefits]

# ============================================================================
# SYSTEM PROMPT
# ============================================================================

SYSTEM_PROMPT = """You are Favour, healthcare assistant for Leadway Health in Nigeria.

KEEP IT SHORT - Max 2-4 lines.

GREETING (ONLY ONCE):
Good morning! I'm Favour from Leadway Health. How can I help?

1. Get My Member ID
2. Check My Benefit Limits
3. Find My Dependants
4. Talk to Agent

AVAILABLE TOOLS:
- lookup_member_for_id: Search by phone number
- lookup_member_by_email: Search by email
- get_dependants: Get list of dependants (requires enrollee ID)
- check_benefits: Check benefit limits (requires enrollee ID and benefit type)

CRITICAL TOOL CALLING RULES:
- When you see 11-digit phone (07x/08x/09x) → IMMEDIATELY call lookup_member_for_id
- When you see email with @ → IMMEDIATELY call lookup_member_by_email
- When user asks about dependants → call get_dependants with their enrollee ID
- When user asks about benefits → call check_benefits with their enrollee ID
- If you have enrollee ID from earlier in conversation → use it
- DO NOT ask for verification - just call tools

CRITICAL RESPONSE FORMATTING:
- Benefits MUST show as: "Benefit Limit: N..." and "Benefit Balance: N..."
- NEVER use other formats like "Balance N..." or "Limit: N..." or "(Limit: N...)"
- Keep responses SHORT and DIRECT

OPTION 2 - CHECK BENEFIT LIMITS:
When member selects option 2 or asks about benefits:
1. If you don't have their enrollee ID → get it first (phone/email lookup)
2. Show benefit sub-menu:
   "Which benefit would you like to check?
   
   1. Lens/Frames
   2. Dental
   3. Chronic Medications
   4. Surgery
   5. Major Disease
   6. All Benefits"
   
3. When they select → call check_benefits with enrollee ID and benefit type
4. CRITICAL: Always format benefit responses EXACTLY like this:

   For single benefit:
   "Dental:
   Benefit Limit: N30,000.00
   Benefit Balance: N25,000.00"
   
   For multiple benefits:
   "Lens/Frames:
   Benefit Limit: N50,000.00
   Benefit Balance: N50,000.00
   
   Dental:
   Benefit Limit: N30,000.00
   Benefit Balance: N25,000.00"
   
   NEVER say "Balance N..." or "Limit: N..." - ALWAYS use "Benefit Limit:" and "Benefit Balance:"

OPTION 3 - DEPENDANTS:
When member asks about dependants:
1. If you have their enrollee ID → IMMEDIATELY call get_dependants
2. Return list: "[Name] ([Relationship]) - [ID]"

Keep responses SHORT and DIRECT. Use their enrollee ID if you already have it from earlier in the conversation."""

# ============================================================================
# BOT - FIXED TOOL CALLING
# ============================================================================

class LeadwayHealthBot:
    def __init__(self):
        # LLM with tools bound
        self.llm_with_tools = ChatAnthropic(
            model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
            temperature=0.7,
            max_tokens=2048,
        ).bind_tools(TOOLS)
        
        # LLM without tools for final response
        self.llm_no_tools = ChatAnthropic(
            model=os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001"),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
            temperature=0.7,
            max_tokens=2048,
        )
        
        self.chat_history = []
        self.last_request_time = 0
        self.min_delay = 12
    
    def _wait_for_rate_limit(self):
        now = time.time()
        time_since_last = now - self.last_request_time
        
        if time_since_last < self.min_delay:
            wait_time = self.min_delay - time_since_last
            print(f"Rate limiting: waiting {wait_time:.1f}s...")
            time.sleep(wait_time)
    
    def normalize_phone_number(self, phone: str) -> list:
        """
        Normalize phone number and return all possible formats to try.
        FIRST format returned is the user's exact input (cleaned of spaces/hyphens only).
        Then other normalized formats.
        
        Input examples:
        - +2348188626141
        - 2348188626141
        - 08188626141
        - 081 88626141
        
        Returns: [user_format, format2, format3, ...]
        """
        # Clean spaces/hyphens but keep the basic structure
        user_format = phone.replace(' ', '').replace('-', '').replace('(', '').replace(')', '')
        
        # Start with user's exact format
        formats_to_try = [user_format]
        
        # Now generate alternative formats
        clean = user_format.replace('+', '')  # Remove + for processing
        
        # If starts with 234 (international)
        if clean.startswith('234') and len(clean) == 13:
            local = '0' + clean[3:]  # 2348188626141 -> 08188626141
            if local not in formats_to_try:
                formats_to_try.append(local)
            if clean not in formats_to_try:
                formats_to_try.append(clean)
            if f'+{clean}' not in formats_to_try:
                formats_to_try.append(f'+{clean}')
        
        # If starts with +234
        elif clean.startswith('234') and user_format.startswith('+'):
            local = '0' + clean[3:]
            if local not in formats_to_try:
                formats_to_try.append(local)
            if clean not in formats_to_try:
                formats_to_try.append(clean)
        
        # If starts with 0 (local)
        elif clean.startswith('0') and len(clean) == 11:
            intl = '234' + clean[1:]  # 08188626141 -> 2348188626141
            if intl not in formats_to_try:
                formats_to_try.append(intl)
            if f'+{intl}' not in formats_to_try:
                formats_to_try.append(f'+{intl}')
        
        # If it's just 10 digits (missing leading 0 or 234)
        elif len(clean) == 10 and clean[0] in ['7', '8', '9']:
            local = '0' + clean
            intl = '234' + clean
            if local not in formats_to_try:
                formats_to_try.append(local)
            if intl not in formats_to_try:
                formats_to_try.append(intl)
            if f'+{intl}' not in formats_to_try:
                formats_to_try.append(f'+{intl}')
        
        return formats_to_try
    
    def process_message(self, message: str) -> str:
        try:
            self._wait_for_rate_limit()
            
            # FORCE TOOL CALLING: Detect phone numbers in multiple formats
            import re
            
            # Match phone numbers in various formats:
            # +2348188626141, 2348188626141, 08188626141, 081 88626141, etc.
            phone_patterns = [
                r'\+?234\s*[7-9]\d{2}\s*\d{3}\s*\d{4}',  # +234XXXXXXXXX or 234XXXXXXXXX
                r'\b0[7-9]\d{1}\s*\d{3}\s*\d{4}\b',       # 08XXXXXXXXX
                r'\b[7-9]\d{9}\b'                          # XXXXXXXXXX (10 digits)
            ]
            
            email_pattern = r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'
            
            found_phone = None
            for pattern in phone_patterns:
                match = re.search(pattern, message)
                if match:
                    found_phone = match.group()
                    break
            
            found_email = re.search(email_pattern, message)
            
            # If phone number detected, force the tool call
            if found_phone and not found_email:
                print(f"[SYSTEM] Phone number detected, looking up member...")
                
                # Normalize and get all formats to try
                formats_to_try = self.normalize_phone_number(found_phone)
                
                # Try each format until one works
                result = None
                for phone_format in formats_to_try:
                    print(f"[SYSTEM] Trying format: {phone_format}")
                    
                    for tool in TOOLS:
                        if tool.name == "lookup_member_for_id":
                            result = tool.invoke({"phone_number": phone_format})
                            
                            if result.get("found") and result.get("enrollee_id"):
                                print(f"[SYSTEM] ✓ Member found")
                                break
                    
                    if result and result.get("found") and result.get("enrollee_id"):
                        break
                
                # Format response - CLEAN and SIMPLE
                if result and result.get("found"):
                    enrollee_id = result.get("enrollee_id")
                    name = result.get("name")
                    
                    if enrollee_id and name:
                        response_text = f"{name}, your Member ID is: {enrollee_id}"
                    elif enrollee_id:
                        response_text = f"Your Member ID is: {enrollee_id}"
                    else:
                        response_text = "Found your record but Member ID is not available. Please contact support."
                else:
                    response_text = f"No member found with phone number {found_phone}. Please verify or provide your email."
                
                # Update history
                self.chat_history.append(HumanMessage(content=message))
                self.chat_history.append(AIMessage(content=response_text))
                
                if len(self.chat_history) > 10:
                    self.chat_history = self.chat_history[-10:]
                
                self.last_request_time = time.time()
                return response_text
            
            # Build messages for Claude
            messages = self.chat_history + [HumanMessage(content=message)]
            
            # System prompt
            system_message = SYSTEM_PROMPT
            
            # Call Claude with tools
            response = self.llm_with_tools.invoke(messages)
            
            # Check if Claude wants to use tools
            if hasattr(response, 'tool_calls') and response.tool_calls:
                print(f"[BOT] Claude calling {len(response.tool_calls)} tool(s)...")
                
                # Execute tools and collect results
                tool_results = []
                for tool_call in response.tool_calls:
                    tool_name = tool_call['name']
                    tool_args = tool_call['args']
                    
                    print(f"[BOT] Calling: {tool_name}({tool_args})")
                    
                    # Find and execute tool
                    for tool in TOOLS:
                        if tool.name == tool_name:
                            result = tool.invoke(tool_args)
                            tool_results.append({
                                "tool_name": tool_name,
                                "result": result
                            })
                            break
                
                # Build context for final response
                tool_context = "\n".join([
                    f"Tool {tr['tool_name']} returned: {json.dumps(tr['result'])}"
                    for tr in tool_results
                ])
                
                # Get final response from Claude WITHOUT tools
                final_messages = messages + [
                    AIMessage(content=f"I called the tools. Here are the results:\n{tool_context}\n\nNow I'll respond to the user.")
                ]
                
                final_response = self.llm_no_tools.invoke(final_messages)
                response_text = final_response.content
                
            else:
                # No tools needed, use response directly
                response_text = response.content
            
            # Update history
            self.chat_history.append(HumanMessage(content=message))
            self.chat_history.append(AIMessage(content=response_text))
            
            # Trim history
            if len(self.chat_history) > 10:
                self.chat_history = self.chat_history[-10:]
            
            self.last_request_time = time.time()
            
            return response_text
            
        except Exception as e:
            print(f"[BOT] ERROR: {e}")
            import traceback
            traceback.print_exc()
            return "Sorry, I encountered an error. Please try again."

# ============================================================================
# TEST MODE
# ============================================================================

def test_bot():
    print("=" * 60)
    print("Leadway Health Bot - FIXED VERSION")
    print("=" * 60)
    print()
    
    bot = LeadwayHealthBot()
    
    while True:
        user_input = input("\nYou: ").strip()
        
        if not user_input:
            continue
        
        if user_input.lower() in ['quit', 'exit']:
            print("\nGoodbye!")
            break
        
        print("Bot: ", end="", flush=True)
        response = bot.process_message(user_input)
        print(response)

if __name__ == "__main__":
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ERROR: ANTHROPIC_API_KEY not found")
        exit(1)
    
    test_bot()
