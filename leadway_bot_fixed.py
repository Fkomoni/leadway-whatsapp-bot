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


# Shown only when the live API call fails entirely
FALLBACK_GYMS = [
    {"id": "", "name": "i-Fitness Centre", "address": "Various Lagos locations",
     "state": "Lagos", "lga": "", "phone": "07002248772", "email": "info@ifitness.com.ng"},
    {"id": "", "name": "Bodyline Fitness", "address": "Various Lagos locations",
     "state": "Lagos", "lga": "", "phone": "", "email": ""},
    {"id": "", "name": "Gym+ Abuja", "address": "Various Abuja locations",
     "state": "FCT", "lga": "", "phone": "", "email": ""},
]


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
def get_gym_spa_providers(enrollee_id: str, state: str) -> dict:
    """
    Find gyms and spas covered under a member's health plan in a given Nigerian state.
    Internally resolves the member's SchemeID from their bio data, then fetches
    the wellness provider list for that plan.

    Args:
        enrollee_id: Member's enrollee ID (e.g., 21000645/0)
        state: Nigerian state name, e.g. "Lagos", "Abuja", "Rivers"
    """
    try:
        if not re.match(r'^[A-Za-z0-9/\-]+$', enrollee_id):
            return {"found": False, "message": "Invalid enrollee ID format"}

        # Step 1: resolve SchemeID — must NOT URL-encode the / in enrollee ID
        bio_r = api_client.get_raw(
            f"/EnrolleeProfile/GetEnrolleeBioDataByEnrolleeID?enrolleeid={enrollee_id}"
        )
        if not bio_r.ok or not bio_r.text.strip():
            return {"found": False, "message": "Could not retrieve member plan details"}

        bio = _extract_inner(bio_r.json())
        member = bio[0] if isinstance(bio, list) else bio
        scheme_id = member.get("Member_PlanID")
        if not scheme_id:
            return {"found": False, "message": "Member plan ID not found"}

        # Step 2: fetch wellness providers
        gym_r = api_client.get_raw(
            f"/ListValues/GetGeneralGymandSpaByPlanCode"
            f"?SchemeID={scheme_id}&MinimumID=0&NoOfRecords=500&pageSize=500"
        )
        if not gym_r.ok:
            providers = [p for p in FALLBACK_GYMS if p["state"].lower() == state.lower()]
            return {"found": True, "fallback": True, "count": len(providers), "providers": providers}

        raw = _extract_inner(gym_r.json())
        if not isinstance(raw, list):
            raw = raw.get("providers") or raw.get("Providers") or raw.get("items") or raw.get("Items") or []

        # Normalise field names
        normalised = []
        for x in (raw or []):
            name_val = next(
                (f for f in [x.get("provider"), x.get("name"), x.get("Name"),
                              x.get("ProviderName"), x.get("GymName"), x.get("SpaName"),
                              x.get("FacilityName"), x.get("HospitalName")]
                 if isinstance(f, str) and f.strip()), "")
            try:
                lat = float(x.get("latitude") or x.get("lat") or x.get("Latitude") or 0)
                lng = float(x.get("longitude") or x.get("lng") or x.get("Longitude") or 0)
            except (TypeError, ValueError):
                lat, lng = 0.0, 0.0
            normalised.append({
                "id": str(x.get("ProviderCode") or x.get("providerCode") or x.get("NamasNo") or
                          x.get("provider_id") or x.get("ProviderID") or ""),
                "name": name_val.strip(),
                "address": str(x.get("ProviderAddress") or x.get("address") or x.get("Address") or "").strip(),
                "state": str(x.get("StateOfOrigin") or x.get("state") or x.get("State") or "").strip(),
                "lga": str(x.get("CityOfOrigin") or x.get("region") or x.get("town") or
                           x.get("City") or x.get("LGA") or "").strip(),
                "phone": str(x.get("phone1") or x.get("Phone1") or x.get("phone") or
                             x.get("PhoneNo") or x.get("phone2") or "").strip(),
                "email": str(x.get("email") or x.get("Email") or "").strip(),
                "lat": lat if lat != 0 else None,
                "lng": lng if lng != 0 else None,
            })

        # Deduplicate by name+address (API returns one row per service line)
        seen: Dict[str, dict] = {}
        for p in normalised:
            if not p["name"]:
                continue
            key = f"{p['name'].lower()}|{p['address'].lower()}"
            def _score(x):
                return (4 if x["lat"] is not None else 0) + (2 if x["phone"] else 0) + (1 if x["address"] else 0)
            if key not in seen or _score(p) > _score(seen[key]):
                seen[key] = p

        # Filter junk rows
        all_providers = [
            p for p in seen.values()
            if p["name"]
            and not re.search(r"dummy|deactivated|referrals only", p["name"], re.IGNORECASE)
            and not re.match(r"^[09]+$", p["id"])
        ]

        # Filter by requested state
        in_state = [p for p in all_providers if p["state"].lower() == state.lower()]
        providers_to_return = in_state if in_state else all_providers

        return {
            "found": True,
            "fallback": False,
            "planName": member.get("Member_Plan", ""),
            "total": len(all_providers),
            "count": len(providers_to_return),
            "stateFilter": state,
            "matchedState": bool(in_state),
            "providers": providers_to_return,
        }

    except Exception as e:
        print(f"[ERROR] get_gym_spa_providers: {e}")
        providers = [p for p in FALLBACK_GYMS if p["state"].lower() == state.lower()]
        return {"found": True, "fallback": True, "count": len(providers), "providers": providers}


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
    get_gym_spa_providers,
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
5. Find Gyms & Spas
6. Talk to Agent

AVAILABLE TOOLS:
- lookup_member_for_id: Search by phone number
- lookup_member_by_email: Search by email
- get_dependants: Get list of dependants (requires enrollee ID)
- check_benefits: Check benefit limits (requires enrollee ID and benefit type)
- check_annual_screening_eligibility: Check eligibility + get package/tests (requires enrollee ID)
- get_screening_providers: List screening providers in a state (requires enrollee ID + state)
- book_annual_screening: Book appointment (requires enrollee ID, provider ID, date YYYY-MM-DD)
- get_gym_spa_providers: Find gyms/spas covered by member's plan (requires enrollee ID + state)

CRITICAL TOOL CALLING RULES:
- When you see 11-digit phone (07x/08x/09x) → IMMEDIATELY call lookup_member_for_id
- When you see email with @ → IMMEDIATELY call lookup_member_by_email
- When user asks about dependants → call get_dependants with their enrollee ID
- When user asks about benefits → call check_benefits with their enrollee ID
- When user asks about annual screening → follow OPTION 4 flow below
- When user asks about gyms, spas, fitness, wellness → follow OPTION 5 flow below
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

OPTION 5 - FIND GYMS & SPAS:
Step-by-step flow:

Step 1 - GET STATE:
- If you don't have enrollee ID → get it first (phone/email lookup)
- Ask: "Which state would you like to find a gym or spa in?"

Step 2 - FETCH & SHOW:
- Call get_gym_spa_providers with enrollee ID and state
- If fallback=true → tell member the list may not be complete
- If matchedState=false → tell member no providers were found in that state and show all available
- Show numbered list (max 10), format:
  "1. [name]
      [address], [lga]
      [phone]
  2. ..."
- If provider has no address/phone, skip those lines
- End with: "These gyms are covered under your [planName] plan."

Step 3 - DONE:
- No booking needed for gyms — member just walks in with their member card

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
