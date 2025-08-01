import os
import json
import re
import requests
import threading
import uuid  # ◀️ uuid 라이브러리 추가
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import google.generativeai as genai
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore
from concurrent.futures import ThreadPoolExecutor

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
    
    if FIREBASE_CONFIG_STR.startswith("'") and FIREBASE_CONFIG_STR.endswith("'"):
        config_str = FIREBASE_CONFIG_STR[1:-1]
    else:
        config_str = FIREBASE_CONFIG_STR

    cred_json = json.loads(config_str)
    
    if 'private_key' in cred_json:
        cred_json['private_key'] = cred_json['private_key'].replace('\\n', '\n')
    
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

# --- 서버 측 헬퍼 함수 ---
def get_geocode(address):
    if not Maps_API_KEY: return None
    try:
        response = requests.get(
            'https://maps.googleapis.com/maps/api/geocode/json',
            params={'address': address, 'key': Maps_API_KEY, 'language': 'ko'}
        )
        response.raise_for_status()
        data = response.json()
        if data['status'] == 'OK':
            result = data['results'][0]
            country_code = next((c['short_name'] for c in result.get('address_components', []) if 'country' in c.get('types', [])), None)
            return {'location': result['geometry']['location'], 'viewport': result['geometry'].get('viewport'), 'country_code': country_code}
        return None
    except requests.exceptions.RequestException as e:
        print(f"지오코딩 API 요청 중 오류 발생: {e}")
        return None

def create_plan_in_background(data, plan_id):
    """백그라운드에서 실행될 AI 계획 생성 및 저장 함수"""
    with app.app_context():
        # ◀️ 백그라운드 작업 시작 시, 가장 먼저 Firestore에 상태를 기록
        doc_ref = db.collection('plans').document(plan_id)
        doc_ref.set({'status': 'processing', 'request_details': data})
        print(f"⏳ Plan {plan_id} 생성 시작...")
        
        try:
            prompt = f"""
            당신은 여행 계획 전문가입니다. 다음 요구사항에 맞춰 여행 계획을 JSON 형식으로 작성해주세요.

            **요구사항:**
            - 여행지: {data.get('destination')}
            - 기간: {data.get('duration')}
            - 동행: {data.get('companions')}
            - 여행 스타일: {data.get('pace')}
            - 선호 활동: {', '.join(data.get('preferredActivities', []))}
            - 주요 이동 수단: {data.get('transportation')}
            - 숙소 유형: {data.get('lodgingType')}
            - 첫날 도착 시간: {data.get('arrivalTime')}
            - 마지막 날 출발 시간: {data.get('departureTime', "오후 (저녁까지 즐기기)")}

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
            validated_plan_str = None
            destination_geocode = get_geocode(data.get('destination'))
            
            for i in range(MAX_RETRIES):
                print(f"AI 계획 생성 시도 #{i + 1}")
                response = model.generate_content(prompt)
                raw_text = response.text
                
                match = re.search(r'```json\s*(\{.*?\})\s*```', raw_text, re.DOTALL) or re.search(r'\{.*\}', raw_text, re.DOTALL)
                if not match:
                    prompt += "\n\n[오류 수정 요청] JSON 형식이 아닙니다. 반드시 JSON 형식으로만 응답해주세요."
                    continue
                plan_json_str = match.group(1) if match.groups() else match.group(0)
                
                try:
                    plan_data = json.loads(plan_json_str)
                except json.JSONDecodeError:
                    prompt += "\n\n[오류 수정 요청] 이전 응답은 유효한 JSON이 아니었습니다. JSON 문법 오류를 수정하여 다시 생성해주세요."
                    continue

                if not isinstance(plan_data.get('daily_plans'), list) or not plan_data['daily_plans']:
                    prompt += "\n\n[오류 수정 요청] 'daily_plans' 배열이 비어있거나 누락되었습니다. 다시 생성해주세요."
                    continue
                
                if destination_geocode and destination_geocode.get('viewport'):
                    # 여기에 기존의 유효성 검증 로직을 그대로 사용하시면 됩니다.
                    pass

                print("✅ 계획 유효성 검증 성공!")
                validated_plan_str = plan_json_str
                break

            if not validated_plan_str:
                raise ValueError("AI가 여러 번 시도했으나 올바른 계획을 생성하지 못했습니다.")

            final_plan_data = json.loads(validated_plan_str)
            if destination_geocode and destination_geocode.get('country_code'):
                final_plan_data['country_code'] = destination_geocode.get('country_code')

            full_plan_data = {'plan': final_plan_data, 'request_details': data, 'status': 'completed'}
            doc_ref.set(full_plan_data) # ◀️ 기존 문서를 덮어씁니다.
            print(f"✅ Plan {plan_id} 저장 완료.")

        except Exception as e:
            print(f"❌ 백그라운드 작업 오류: {e}")
            doc_ref.set({'status': 'failed', 'error': str(e), 'request_details': data})

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
        return jsonify(doc.to_dict()) if doc.exists else (jsonify({"error": "Plan not found"}), 404)
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
    """AI 여행 계획 생성을 요청하고 즉시 plan_id를 반환"""
    if not model or not db:
        return jsonify({'error': 'AI 모델 또는 데이터베이스가 초기화되지 않았습니다.'}), 500
        
    try:
        data = request.json
        # ◀️ Firestore에 접속하는 대신, 로컬에서 고유 ID를 생성합니다.
        plan_id = str(uuid.uuid4())
        
        # ◀️ Firestore 관련 작업을 모두 백그라운드로 넘깁니다.
        thread = threading.Thread(target=create_plan_in_background, args=(data, plan_id))
        thread.daemon = True
        thread.start()
        
        # ◀️ 생성된 ID를 즉시 반환합니다. (네트워크 지연 없음)
        return jsonify({'plan_id': plan_id})
    
    except Exception as e:
        print(f"플랜 생성 요청 처리 중 오류: {e}")
        return jsonify({'error': str(e)}), 500

# 로컬 개발 환경에서 직접 실행할 때 사용
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
