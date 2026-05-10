"""
Leadway Health WhatsApp Bot - FIXED VERSION
============================================
Proper tool calling with correct message flow
"""

import os
import re
import math
import time
import json
import requests
from urllib.parse import quote as _url_quote
from typing import Optional, Dict, List
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
            response = requests.post(
                f"{self.base_url}/ApiUsers/Login",
                json={"Username": self.username, "Password": self.password},
                headers={"Content-Type": "application/json", "User-Agent": "PostmanRuntime/7.51.1"},
                timeout=10
            )
            if response.status_code == 200:
                data = response.json()
                inner = data.get("data") or data.get("Data") or data.get("result") or data.get("Result") or data
                self.token = (
                    inner.get("accessToken") or inner.get("token") or
                    inner.get("AccessToken") or inner.get("Token") or
                    inner.get("bearer") or inner.get("Bearer") or
                    inner.get("bearerToken") or inner.get("BearerToken")
                )
                if self.token:
                    self.token_expiry = time.time() + 5 * 60 * 60  # 5 hours
                    print("✓ Connected to Leadway API")
                    return True
                print(f"ERROR: No token in response. Keys: {list(data.keys())}")
            else:
                print(f"Login failed: {response.status_code} - {response.text}")
        except Exception as e:
            print(f"Login error: {e}")
        return False

    def ensure_authenticated(self):
        if not self.token or time.time() >= self.token_expiry:
            if not self.login():
                raise Exception("Failed to authenticate")

    def _make_request(self, method: str, endpoint: str, params: Dict = None, body: Dict = None) -> requests.Response:
        self.ensure_authenticated()
        url = f"{self.base_url}/{endpoint}"
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "PostmanRuntime/7.51.1",
        }
        r = requests.request(method, url, params=params, json=body, headers=headers, timeout=10)
        if r.status_code == 401:
            self.token = None
            self.ensure_authenticated()
            headers["Authorization"] = f"Bearer {self.token}"
            r = requests.request(method, url, params=params, json=body, headers=headers, timeout=10)
        return r

    def get(self, endpoint: str, params: Dict = None) -> Optional[Dict]:
        try:
            r = self._make_request("GET", endpoint, params=params)
            return r.json() if r.status_code == 200 else None
        except Exception as e:
            print(f"Request error: {e}")
            return None

    def post(self, endpoint: str, body: Dict = None) -> Optional[Dict]:
        try:
            r = self._make_request("POST", endpoint, body=body)
            return r.json() if r.status_code == 200 else None
        except Exception as e:
            print(f"Request error: {e}")
            return None

    def get_raw(self, path: str) -> requests.Response:
        """GET with a pre-built path — slashes are NOT percent-encoded (required for enrollee IDs)."""
        self.ensure_authenticated()
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "User-Agent": "PostmanRuntime/7.51.1",
        }
        url = f"{self.base_url}{path}"
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code == 401:
            self.token = None
            self.ensure_authenticated()
            headers["Authorization"] = f"Bearer {self.token}"
            r = requests.get(url, headers=headers, timeout=10)
        return r


def _extract_inner(data):
    """Unwrap the API's status/result envelope."""
    if not isinstance(data, dict):
        return data
    return data.get("data") or data.get("Data") or data.get("result") or data.get("Result") or data


def _distance_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371
    to_rad = lambda d: d * math.pi / 180
    d_lat = to_rad(lat2 - lat1)
    d_lng = to_rad(lng2 - lng1)
    a = (math.sin(d_lat / 2) ** 2 +
         math.cos(to_rad(lat1)) * math.cos(to_rad(lat2)) * math.sin(d_lng / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


api_client = LeadwayAPIClient()

PROVIDER_ENDPOINTS = {
    "hospital": "GetProvidersByPlanCode",
    "eye":      "GetEyeClinicByPlanCode",
    "dental":   "GetDentalClinicByPlanCode",
    "wellness": "GetGeneralGymandSpaByPlanCode",
}

_provider_cache: Dict[str, tuple] = {}
_PROVIDER_CACHE_TTL = 10 * 60  # 10 minutes


def _provider_score(p: dict) -> int:
    return (4 if p["lat"] is not None else 0) + (2 if p["phone"] else 0) + (1 if p["address"] else 0)


def _normalise_providers(raw_list) -> List[dict]:
    """Normalise, dedupe, and filter a raw Prognosis provider list."""
    normalised = []
    for x in (raw_list or []):
        name = ""
        for f in [x.get("provider"), x.get("name"), x.get("Name"), x.get("ProviderName"),
                  x.get("providerName"), x.get("provider_name"), x.get("Provider_Name"),
                  x.get("ClinicName"), x.get("HospitalName"), x.get("FacilityName"),
                  x.get("GymName"), x.get("SpaName")]:
            if isinstance(f, str) and f.strip():
                name = f.strip()
                break
        # Use `or`, not `and`/`??` — provider_id=0 is a "missing" sentinel
        id_val = (x.get("ProviderCode") or x.get("providerCode") or
                  x.get("NamasNo") or x.get("namasNo") or x.get("namas_no") or
                  x.get("provider_id") or x.get("ProviderID") or x.get("ProviderId") or
                  x.get("Id") or x.get("ID") or "")
        try:
            lat = float(x.get("latitude") or x.get("lat") or x.get("Lat") or
                        x.get("Latitude") or x.get("GeoLat") or 0)
            lng = float(x.get("longitude") or x.get("lng") or x.get("long") or
                        x.get("Long") or x.get("Longitude") or x.get("GeoLng") or 0)
        except (TypeError, ValueError):
            lat, lng = 0.0, 0.0
        normalised.append({
            "id":      str(id_val),
            "name":    name,
            "address": str(x.get("ProviderAddress") or x.get("provider_address") or
                           x.get("address") or x.get("Address") or "").strip(),
            "state":   str(x.get("StateOfOrigin") or x.get("state") or x.get("State") or
                           x.get("ProviderState") or x.get("Provider_State") or "").strip(),
            "lga":     str(x.get("CityOfOrigin") or x.get("region") or x.get("town") or
                           x.get("Town") or x.get("City") or x.get("LGA") or "").strip(),
            "phone":   str(x.get("phone1") or x.get("Phone1") or x.get("phone") or
                           x.get("Phone") or x.get("PhoneNo") or x.get("phone2") or "").strip(),
            "email":   str(x.get("email") or x.get("Email") or x.get("provider_email") or "").strip(),
            "lat":     lat if lat != 0 else None,
            "lng":     lng if lng != 0 else None,
        })
    # Deduplicate by name|address — keep row with most info
    seen: Dict[str, dict] = {}
    for p in normalised:
        if not p["name"]:
            continue
        key = f"{p['name'].lower()}|{p['address'].lower()}"
        if key not in seen or _provider_score(p) > _provider_score(seen[key]):
            seen[key] = p
    # Filter junk
    return [
        p for p in seen.values()
        if p["name"]
        and not re.search(r"dummy|deactivated|referrals only", p["name"], re.IGNORECASE)
        and p["id"]
        and not re.match(r"^[09]+$", p["id"])
    ]

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
    r = api_client.get_raw(f"/EnrolleeProfile/GetEnrolleeDependantsByEnrolleeID?enrolleeid={enrollee_id}")
    if not r.ok or not r.text.strip():
        return {"found": False, "dependants": []}
    result = r.json()
    
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

@tool
def check_annual_screening_eligibility(enrollee_id: str) -> dict:
    """
    Check if a member is eligible for their annual health screening and return
    the package name and list of tests included.

    Args:
        enrollee_id: Member's enrollee ID (e.g., 21000645/0)
    """
    try:
        r = api_client.get_raw(f"/AnnualScreening/GetScreeningPackage?EnrolleeID={enrollee_id}")
        if r.status_code in (404, 405):
            r = api_client._make_request("POST", "AnnualScreening/GetScreeningPackage",
                                         body={"EnrolleeID": enrollee_id})
        if r.status_code in (404, 501):
            r = api_client.get_raw(f"/Production/GetHealthCheckEligibility?enrolleeid={enrollee_id}")
        if not r.ok:
            return {"found": False, "message": "Unable to check eligibility"}

        data = r.json()
        inner = _extract_inner(data)
        o = inner[0] if isinstance(inner, list) else inner

        raw_tests = o.get("tests") or o.get("Tests") or []
        tests = [
            {"testName": t, "testCode": ""} if isinstance(t, str) else {
                "testName": t.get("testname") or t.get("testName") or t.get("TestName", ""),
                "testCode": t.get("testcode") or t.get("testCode") or t.get("TestCode", ""),
            }
            for t in raw_tests
        ]

        return {
            "found": True,
            "isEligible": bool(o.get("isEligible") or o.get("IsEligible")),
            "memberName": o.get("memberName") or o.get("MemberName", ""),
            "memberEmail": o.get("memberEmail") or o.get("MemberEmail", ""),
            "age": o.get("age") or o.get("Age"),
            "gender": o.get("gender") or o.get("Gender", ""),
            "packageName": o.get("packageName") or o.get("PackageName", ""),
            "nextEligibleDate": o.get("nextEligibleDate") or o.get("NextEligibleDate"),
            "tests": tests,
        }
    except Exception as e:
        print(f"[ERROR] check_annual_screening_eligibility: {e}")
        return {"found": False, "error": str(e)}


@tool
def get_screening_providers(enrollee_id: str, state: str) -> dict:
    """
    Get the list of health screening providers available in a given Nigerian state.

    Args:
        enrollee_id: Member's enrollee ID
        state: Nigerian state name, e.g. "Lagos", "Abuja", "Rivers"
    """
    try:
        r = api_client.get_raw(
            f"/AnnualScreening/GetScreeningProviders?EnrolleeID={enrollee_id}&State={_url_quote(state)}"
        )
        if r.status_code in (404, 405):
            r = api_client._make_request("POST", "AnnualScreening/GetScreeningProviders",
                                         body={"EnrolleeID": enrollee_id, "State": state})
        if r.status_code in (404, 501):
            r = api_client.get_raw(
                f"/Production/GetHealthCheckProviders?enrolleeid={enrollee_id}&Region={_url_quote(state)}"
            )
        if not r.ok:
            return {"found": False, "providers": [], "message": f"No providers found in {state}"}

        data = r.json()
        inner = _extract_inner(data)
        raw_list = inner if isinstance(inner, list) else (inner.get("providers") or inner.get("Providers") or [])

        providers = []
        for x in raw_list:
            name = (x.get("provider") or x.get("name") or x.get("Name") or x.get("ProviderName", "")).strip()
            pid = str(x.get("ProviderCode") or x.get("providerCode") or x.get("NamasNo") or
                      x.get("ProviderID") or x.get("provider_id", ""))
            if not name:
                continue
            if re.search(r"dummy|deactivated|referrals only", name, re.IGNORECASE):
                continue
            if re.match(r"^[09]+$", pid):
                continue
            providers.append({
                "id": pid,
                "name": name,
                "address": (x.get("ProviderAddress") or x.get("address", "")).strip(),
                "town": (x.get("CityOfOrigin") or x.get("region") or x.get("town") or x.get("City", "")).strip(),
                "state": (x.get("StateOfOrigin") or x.get("state", "")).strip(),
                "phone": (x.get("phone1") or x.get("phone") or x.get("phone2", "")).strip(),
                "email": (x.get("email") or x.get("Email", "")).strip(),
            })

        return {"found": True, "count": len(providers), "providers": providers}
    except Exception as e:
        print(f"[ERROR] get_screening_providers: {e}")
        return {"found": False, "providers": [], "error": str(e)}


@tool
def book_annual_screening(enrollee_id: str, provider_id: str, preferred_date: str) -> dict:
    """
    Book an annual health screening appointment for a member.

    Args:
        enrollee_id: Member's enrollee ID
        provider_id: 7-digit provider code from get_screening_providers
        preferred_date: Date in YYYY-MM-DD format
    """
    try:
        r = api_client._make_request("POST", "EnrolleeProfile/BookHealthCheck", body={
            "EnrolleeID": enrollee_id,
            "ProviderID": provider_id,
            "PreferredDate": preferred_date,
            "NotifyProvider": False,
        })
        data = r.json()
        inner = _extract_inner(data)
        o = inner[0] if isinstance(inner, list) else inner

        if not (o.get("Success") or o.get("success")):
            return {"success": False, "message": o.get("Message") or o.get("message", "Booking failed")}

        raw_tests = o.get("Tests") or o.get("tests") or []
        tests = [t if isinstance(t, str) else (t.get("testname") or t.get("testName") or t.get("TestName", ""))
                 for t in raw_tests]

        return {
            "success": True,
            "paCode": o.get("PACode") or o.get("pacode") or o.get("preauthorizationcode", ""),
            "visitId": o.get("VisitID") or o.get("visitId", ""),
            "memberName": o.get("MemberName") or o.get("membername", ""),
            "providerName": o.get("ProviderName") or o.get("providername", ""),
            "providerEmail": o.get("EmailAddress") or o.get("emailaddress") or o.get("ProviderEmail", ""),
            "scheduledDate": o.get("ScheduledDate") or o.get("scheduleddate", ""),
            "expiryDate": o.get("ExpiryDate") or o.get("expirydate", ""),
            "packageName": o.get("PackageName") or o.get("packagename", ""),
            "tests": tests,
            "instructions": o.get("Instructions") or o.get("instructions", ""),
        }
    except Exception as e:
        print(f"[ERROR] book_annual_screening: {e}")
        return {"success": False, "error": str(e)}


@tool
def cancel_annual_screening(visit_id: str) -> dict:
    """
    Cancel an existing annual health screening booking.

    Args:
        visit_id: The visit ID returned when the booking was made
    """
    try:
        r = api_client._make_request("GET", "EnrolleeProfile/deletehealthcheck",
                                     params={"visitid": visit_id})
        data = None
        try:
            data = r.json()
        except Exception:
            pass
        inner = _extract_inner(data) if data else None
        success = r.ok and ((data and data.get("status") == 200) or isinstance(inner, list))
        return {
            "success": success,
            "message": "Booking cancelled successfully" if success else "Cancellation failed",
        }
    except Exception as e:
        print(f"[ERROR] cancel_annual_screening: {e}")
        return {"success": False, "error": str(e)}


@tool
def get_network_providers(enrollee_id: str, provider_type: str, state: str) -> dict:
    """
    Find in-network providers covered under a member's plan.
    Handles hospitals, eye clinics, dental clinics, and wellness centres (gyms/spas).
    Results are cached for 10 minutes per (type, plan) pair.

    Args:
        enrollee_id:   Member's enrollee ID (e.g., 21000645/0)
        provider_type: "hospital", "eye", "dental", or "wellness"
        state:         Nigerian state to filter by, e.g. "Lagos", "Abuja", "Rivers"
    """
    provider_type = provider_type.lower().strip()
    if provider_type not in PROVIDER_ENDPOINTS:
        return {"found": False,
                "message": f"Unknown type '{provider_type}'. Use: hospital, eye, dental, wellness"}

    if not re.match(r'^[A-Za-z0-9/\-]+$', enrollee_id):
        return {"found": False, "message": "Invalid enrollee ID format"}

    _RETRY_MSG = ("We're unable to retrieve the provider list at the moment. "
                  "Please try again in a few minutes.")
    try:
        # Step 1: resolve SchemeID from bio data (/ must not be URL-encoded)
        bio_r = api_client.get_raw(
            f"/EnrolleeProfile/GetEnrolleeBioDataByEnrolleeID?enrolleeid={enrollee_id}"
        )
        if not bio_r.ok or not bio_r.text.strip():
            return {"found": False, "message": _RETRY_MSG}

        bio = _extract_inner(bio_r.json())
        member = bio[0] if isinstance(bio, list) else bio
        scheme_id = member.get("Member_PlanID")
        plan_name = member.get("Member_Plan", "")
        if not scheme_id:
            return {"found": False, "message": _RETRY_MSG}

        # Step 2: serve from cache if fresh
        cache_key = f"{provider_type}:{scheme_id}"
        now = time.time()
        cached = _provider_cache.get(cache_key)
        if cached and now - cached[0] < _PROVIDER_CACHE_TTL:
            all_providers = cached[1]
        else:
            # Step 3: fetch from Prognosis
            endpoint = PROVIDER_ENDPOINTS[provider_type]
            prov_r = api_client.get_raw(
                f"/ListValues/{endpoint}"
                f"?SchemeID={scheme_id}&MinimumID=0&NoOfRecords=500&pageSize=500"
            )
            if not prov_r.ok:
                return {"found": False, "message": _RETRY_MSG}

            raw = _extract_inner(prov_r.json())
            if not isinstance(raw, list):
                raw = (raw.get("providers") or raw.get("Providers") or
                       raw.get("records") or raw.get("Records") or
                       raw.get("list") or raw.get("List") or
                       raw.get("items") or raw.get("Items") or [])

            all_providers = _normalise_providers(raw)
            _provider_cache[cache_key] = (now, all_providers)

        # Step 4: filter by state
        in_state = [p for p in all_providers if p["state"].lower() == state.lower()]
        providers_to_return = in_state if in_state else all_providers

        return {
            "found": True,
            "providerType": provider_type,
            "planName": plan_name,
            "total": len(all_providers),
            "count": len(providers_to_return),
            "stateFilter": state,
            "matchedState": bool(in_state),
            "providers": providers_to_return,
        }

    except Exception as e:
        print(f"[ERROR] get_network_providers: {e}")
        return {"found": False, "message": _RETRY_MSG}


# cancel_annual_screening is intentionally excluded from TOOLS.
# Members cannot trigger cancellations — supervisors use the supervisor console.
TOOLS = [
    lookup_member_for_id,
    lookup_member_by_email,
    get_dependants,
    check_benefits,
    check_annual_screening_eligibility,
    get_screening_providers,
    book_annual_screening,
    get_network_providers,
]

# ============================================================================
# SYSTEM PROMPT
# ============================================================================

SYSTEM_PROMPT = """You are Favour, healthcare assistant for Leadway Health in Nigeria.

KEEP IT SHORT - Max 2-4 lines per message.

GREETING (ONLY ONCE):
Good morning! I'm Favour from Leadway Health. How can I help?

1. Get My Member ID
2. Check My Benefit Limits
3. Find My Dependants
4. Book Annual Screening
5. Find Providers (Hospitals / Eye / Dental / Gyms & Spas)
6. Talk to Agent

AVAILABLE TOOLS:
- lookup_member_for_id: Search by phone number
- lookup_member_by_email: Search by email
- get_dependants: Get list of dependants (requires enrollee ID)
- check_benefits: Check benefit limits (requires enrollee ID and benefit type)
- check_annual_screening_eligibility: Check eligibility + get package/tests (requires enrollee ID)
- get_screening_providers: List annual screening providers in a state (requires enrollee ID + state)
- book_annual_screening: Book screening appointment (requires enrollee ID, provider ID, date YYYY-MM-DD)
- get_network_providers: Find in-network providers by type and state
    provider_type = "hospital" | "eye" | "dental" | "wellness"
    (requires enrollee ID, provider_type, state)

CRITICAL TOOL CALLING RULES:
- When you see 11-digit phone (07x/08x/09x) → IMMEDIATELY call lookup_member_for_id
- When you see email with @ → IMMEDIATELY call lookup_member_by_email
- When user asks about dependants → call get_dependants with their enrollee ID
- When user asks about benefits → call check_benefits with their enrollee ID
- When user asks about annual screening → follow OPTION 4 flow below
- When user asks about hospitals, clinics, eye, dental, gyms, spas, wellness → follow OPTION 5 flow below
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

OPTION 4 - BOOK ANNUAL SCREENING:
Step-by-step flow:

Step 1 - CHECK ELIGIBILITY:
- Call check_annual_screening_eligibility with enrollee ID
- If isEligible is false → tell member the nextEligibleDate and stop
- If isEligible is true → show:
  "Great news! You're eligible for your [packageName] screening.
  Tests included: [list tests]

  Which state would you like to visit a provider in?"

Step 2 - GET PROVIDERS:
- User gives a state name (e.g. "Lagos", "Abuja")
- Call get_screening_providers with enrollee ID and state
- If no providers → ask them to try a different state
- Show numbered list (max 8 providers), format:
  "1. [name] - [town] ([address])
   2. ..."
  Then ask: "Which provider would you like? Reply with the number."

Step 3 - GET DATE:
- User picks a number → store that provider's id and name
- Ask: "What is your preferred date? (e.g. 2025-08-15)"

Step 4 - CONFIRM & BOOK:
- Call book_annual_screening with enrollee ID, provider id, and date
- On success show:
  "Booking confirmed!
   PA Code: [paCode]
   Provider: [providerName]
   Date: [scheduledDate]
   Expires: [expiryDate]
   Package: [packageName]
   Tests: [tests list]
   [instructions if not empty]"
- On failure → show the error message and ask if they want to try again

CANCELLATION NOTE: Members CANNOT cancel screenings themselves.
If a member asks to cancel → tell them: "To cancel your booking, please call 0800-LEADWAY or speak to a Leadway agent (option 6)."

OPTION 5 - FIND PROVIDERS:
Step-by-step flow:

Step 1 - ASK TYPE:
- If you don't have enrollee ID → get it first (phone/email lookup)
- Show sub-menu:
  "What type of provider are you looking for?
   1. Hospital
   2. Eye Clinic
   3. Dental Clinic
   4. Gym / Spa (Wellness)"

Step 2 - ASK STATE:
- Map choice: 1→"hospital", 2→"eye", 3→"dental", 4→"wellness"
- Ask: "Which state would you like to search in?"

Step 3 - FETCH & SHOW:
- Call get_network_providers with enrollee ID, provider_type, and state
- If found=false → show the message from the API (retry message) and stop
- If matchedState=false → tell member no providers were found in that exact state
  and that you're showing all available under their plan
- Show numbered list (max 10), format:
  "1. [name]
      [address], [lga]
      [phone]
  2. ..."
- Skip address/phone lines if empty
- End with: "These providers are covered under your [planName] plan."

Step 4 - DONE:
- For gyms/spas/hospitals/clinics → member just walks in with their Leadway member card
- No booking required (annual screening has its own separate booking flow)

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

# ============================================================================
# SUPERVISOR CONSOLE  (run with:  python leadway_bot_fixed.py --supervisor)
# Cancellation is a privileged action — members cannot trigger it from the bot.
# ============================================================================

def supervisor_console():
    print("=" * 60)
    print("Leadway Health — Supervisor Console")
    print("=" * 60)
    print()
    print("Commands:")
    print("  cancel <visit_id>   Cancel a member's annual screening booking")
    print("  quit                Exit")
    print()

    while True:
        try:
            cmd = input("Supervisor> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            break

        if not cmd:
            continue
        if cmd.lower() in ("quit", "exit"):
            break

        parts = cmd.split(None, 1)
        action = parts[0].lower()

        if action == "cancel":
            if len(parts) < 2:
                print("Usage: cancel <visit_id>")
                continue
            visit_id = parts[1].strip()
            print(f"Cancelling visit {visit_id} ...")
            result = cancel_annual_screening.invoke({"visit_id": visit_id})
            status = "✓" if result.get("success") else "✗"
            print(f"{status} {result.get('message', result)}")
        else:
            print(f"Unknown command: {action}")


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--supervisor":
        supervisor_console()
    else:
        if not os.getenv("ANTHROPIC_API_KEY"):
            print("ERROR: ANTHROPIC_API_KEY not found")
            exit(1)
        test_bot()
