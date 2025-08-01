import os
import json
import re
import requests
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import google.generativeai as genai
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore
# `concurrent.futures` 모듈은 더 이상 사용되지 않으므로 제거합니다.
# from concurrent.futures import ThreadPoolExecutor

# .env 파일에서 환경 변수를 로드합니다.
load_dotenv()

# Flask 앱을 최상단에서 바로 생성합니다.
app = Flask(__name__)
CORS(app)

# --- 환경 변수 로드 ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
FIREBASE_CONFIG_STR = os.environ.get("FIREBASE_CONFIG")
Maps_API_KEY = os.environ.get("Maps_API_KEY")
KAKAO_API_KEY = os.environ.get("KAKAO_API_KEY")

# --- Firebase Admin SDK 초기화 ---
db = None
try:
    if not FIREBASE_CONFIG_STR:
        raise ValueError("FIREBASE_CONFIG 환경 변수가 설정되지 않았습니다.")
    
    # Cloudtype 환경 변수에서 작은따옴표가 포함될 수 있으므로 제거
    if FIREBASE_CONFIG_STR.startswith("'") and FIREBASE_CONFIG_STR.endswith("'"):
        config_str = FIREBASE_CONFIG_STR[1:-1]
    else:
        config_str = FIREBASE_CONFIG_STR

    # JSON 문자열을 파이썬 딕셔너리로 변환
    cred_json = json.loads(config_str)
    
    # private_key의 '\n' 문자가 '\\n'으로 이스케이프된 경우를 처리
    if 'private_key' in cred_json:
        cred_json['private_key'] = cred_json['private_key'].replace('\\n', '\n')
    
    # Firebase 앱이 이미 초기화되지 않았는지 확인
    if not firebase_admin._apps:
        cred = credentials.Certificate(cred_json)
        firebase_admin.initialize_app(cred)
        
    db = firestore.client()
    print("✅ Firebase 초기화 성공")

except json.JSONDecodeError as json_err:
    print(f"❌ Firebase 설정 JSON 파싱 오류: {json_err}")
    print(f"--- 전달된 FIREBASE_CONFIG 문자열 (처음 100자) ---")
    print(FIREBASE_CONFIG_STR[:100] + "...")
    print("-----------------------------------------")
    db = None
except Exception as e:
    print(f"❌ Firebase 초기화 중 예측하지 못한 오류 발생: {e}")
    db = None


# --- Gemini 모델 초기화 ---
model = None
try:
    if not GEMINI_API_KEY:
        raise ValueError("GEMINI_API_KEY가 .env 파일에 설정되지 않았습니다.")
    genai.configure(api_key=GEMINI_API_KEY)
    generation_config = genai.GenerationConfig(response_mime_type="application/json")
    model = genai.GenerativeModel('gemini-1.5-flash', generation_config=generation_config)
    print("✅ Gemini 모델 초기화 성공")
except Exception as e:
    print(f"❌ Gemini 모델 초기화 중 오류 발생: {e}")
    model = None

# 서버 측 Geocoding 헬퍼 함수는 더 이상 사용하지 않으므로, 더 이상 필요하지 않습니다.
# def get_geocode(address):
#     ...

# --- 라우트(경로) 설정 ---
@app.route('/')
@app.route('/plan/<plan_id>')
def index(plan_id=None):
    return render_template('index.html', Maps_api_key=Maps_API_KEY)

@app.route('/explore')
def explore():
    return render_template('explore.html', plans=[])

@app.route('/get_plan/<plan_id>', methods=['GET'])
def get_plan(plan_id):
    if not db: return jsonify({"error": "DB가 초기화되지 않았습니다."}), 500
    try:
        doc = db.collection('plans').document(plan_id).get()
        return jsonify(doc.to_dict()) if doc.exists else ({"error": "Plan not found"}, 404)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/get_kakao_directions', methods=['POST'])
def get_kakao_directions():
    if not KAKAO_API_KEY: return jsonify({"error": "카카오 API 키가 없습니다."}), 500
    data = request.json
    origin, dest = data.get('origin'), data.get('destination')
    if not origin or not dest: return jsonify({"error": "좌표가 없습니다."}), 400

    headers = {"Authorization": f"KakaoAK {KAKAO_API_KEY}"}
    url = "https://apis-navi.kakaomobility.com/v1/directions"
    params = {"origin": f"{origin['lng']},{origin['lat']}", "destination": f"{dest['lng']},{dest['lat']}"}
    
    try:
        res = requests.get(url, headers=headers, params=params)
        res.raise_for_status()
        result = res.json()
        if result.get('routes'):
            summary = result['routes'][0]['summary']
            return jsonify({"distance": f"{summary['distance']/1000:.1f} km", "duration": f"{summary['duration']//60} 분"})
        return jsonify({"error": "경로를 찾을 수 없습니다."}), 404
    except Exception as e:
        return jsonify({"error": "카카오 API 오류"}), 500

@app.route('/generate', methods=['POST'])
def generate_plan():
    """AI를 이용해 여행 계획을 생성하고, 검증 후 반환하는 핵심 함수"""
    if not model or not db:
        return jsonify({'error': 'AI 모델 또는 데이터베이스가 초기화되지 않았습니다.'}), 500
        
    try:
        data = request.json
        original_prompt = f"""
당신은 여행 계획 전문가입니다. 다음 요구사항에 맞춰 여행 계획을 JSON 형식으로 작성해주세요.

**요구사항:**
- **여행지:** {data.get('destination')}
- **기간:** {data.get('duration')}
- **동행:** {data.get('companions')}
- **여행 스타일:** {data.get('pace')}
- **선호 활동:** {', '.join(data.get('preferredActivities', []))}
- **주요 이동 수단:** {data.get('transportation')}
- **숙소 유형:** {data.get('lodgingType')}
- **첫날 도착 시간:** {data.get('arrivalTime')}
- **마지막 날 출발 시간:** "오후 (저녁까지 즐기기)"

**JSON 출력 형식 (반드시 이 형식을 따라야 합니다):**
{{
  "title": "{data.get('destination')} 맞춤 여행 코스",
  "daily_plans": [
    {{
      "day": 1,
      "theme": "도착 그리고 첫 만남",
      "activities": [
        {{"place": "추천 장소 1", "type": "식사", "description": "장소에 대한 간략한 설명"}},
        {{"place": "추천 장소 2", "type": "관광", "description": "장소에 대한 간략한 설명"}},
        {{"place": "추천 숙소", "type": "숙소", "description": "숙소에 대한 간략한 설명"}}
      ]
    }}
  ]
}}

**규칙:**
- `daily_plans` 배열은 비워두지 마세요.
- 각 `activities` 배열에는 최소 3개 이상의 활동을 포함해주세요.
- `type`은 '식사', '관광', '카페', '쇼핑', '액티비티', '숙소' 중에서 선택하세요.
- 각 날의 마지막 활동은 반드시 `type: '숙소'`여야 합니다. (단, 당일치기 제외)
- 모든 장소 이름은 "{data.get('destination')}" 내에 실제로 존재하는 정확한 명칭을 사용해주세요.
- 장소 이름이 중복되지 않도록 주의해주세요.
"""
        MAX_RETRIES = 2
        final_plan_data = None
        
        # 서버 측 지오코딩 검증 로직을 제거합니다.
        # 이 로직은 클라이언트 측 JavaScript에서 처리하는 것이 훨씬 효율적입니다.
        for i in range(MAX_RETRIES):
            print(f"AI 계획 생성 시도 #{i + 1}")
            response = model.generate_content(original_prompt)
            raw_text = response.text
            
            match = re.search(r'```json\s*(\{.*?\})\s*```', raw_text, re.DOTALL) or re.search(r'\{.*\}', raw_text, re.DOTALL)
            if not match:
                original_prompt += "\n\n[오류 수정 요청] JSON 형식이 아닙니다. 반드시 JSON 형식으로만 응답해주세요."
                continue
            plan_json_str = match.group(1) if match.groups() else match.group(0)
            
            try:
                plan_data = json.loads(plan_json_str)
            except json.JSONDecodeError:
                original_prompt += "\n\n[오류 수정 요청] 이전 응답은 유효한 JSON이 아니었습니다. JSON 문법 오류를 수정하여 다시 생성해주세요."
                continue

            if not isinstance(plan_data.get('daily_plans'), list) or not plan_data['daily_plans']:
                original_prompt += "\n\n[오류 수정 요청] 'daily_plans' 배열이 비어있거나 누락되었습니다. 다시 생성해주세요."
                continue
            
            # 유효성 검증에 성공했으므로 루프를 빠져나갑니다.
            final_plan_data = plan_data
            break

        if not final_plan_data:
            raise ValueError("AI가 여러 번 시도했으나 올바른 계획을 생성하지 못했습니다.")

        # 서버 측 지오코딩 API를 호출하는 코드를 제거했으므로,
        # 이 부분은 주석 처리하거나 제거합니다.
        # destination_geocode = get_geocode(data.get('destination'))
        # if destination_geocode and destination_geocode.get('country_code'):
        #     final_plan_data['country_code'] = destination_geocode.get('country_code')

        full_plan_data = { 'plan': final_plan_data, 'request_details': data }
        doc_ref = db.collection('plans').document()
        doc_ref.set(full_plan_data)
        
        return jsonify({'plan': final_plan_data, 'plan_id': doc_ref.id})
    
    except Exception as e:
        print(f"플랜 생성 중 오류: {e}")
        return jsonify({'error': str(e)}), 500

# 로컬 개발 환경에서 직접 실행할 때 사용
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
