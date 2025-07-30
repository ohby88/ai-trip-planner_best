import os
import json
import re
import requests # API 요청을 위한 라이브러리 추가
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import google.generativeai as genai
from dotenv import load_dotenv
import firebase_admin
from firebase_admin import credentials, firestore

# .env 파일에서 환경 변수 로드
load_dotenv()

app = Flask(__name__)
CORS(app)

# --- 환경 변수 로드 ---
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
FIREBASE_CONFIG_STR = os.environ.get("FIREBASE_CONFIG")
# 👇 [최종 수정] 서버 측 지오코딩을 위한 구글 지도 API 키
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY") 
# --------------------

# --- Firebase Admin SDK 초기화 ---
try:
    if not FIREBASE_CONFIG_STR:
        raise ValueError("FIREBASE_CONFIG 환경 변수가 설정되지 않았습니다.")
    cred_json = json.loads(FIREBASE_CONFIG_STR)
    if 'private_key' in cred_json:
        cred_json['private_key'] = cred_json['private_key'].replace('\\n', '\n')
    cred = credentials.Certificate(cred_json)
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    print("✅ Firebase 초기화 성공")
except Exception as e:
    db = None
    print(f"❌ Firebase 초기화 중 오류 발생: {e}")
# ---------------------------------

# Gemini 모델 초기화
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

# --- 서버 측 지오코딩 헬퍼 함수 ---
def get_geocode(address):
    if not GOOGLE_MAPS_API_KEY:
        print("❌ GOOGLE_MAPS_API_KEY가 설정되지 않아 지오코딩을 건너뜁니다.")
        return None
    try:
        response = requests.get(
            'https://maps.googleapis.com/maps/api/geocode/json',
            params={'address': address, 'key': GOOGLE_MAPS_API_KEY}
        )
        response.raise_for_status()
        data = response.json()
        if data['status'] == 'OK':
            geometry = data['results'][0]['geometry']
            return {
                'location': geometry['location'],
                'viewport': geometry.get('viewport')
            }
        return None
    except requests.exceptions.RequestException as e:
        print(f"지오코딩 API 요청 중 오류 발생: {e}")
        return None
# ------------------------------------

@app.route('/')
@app.route('/plan/<plan_id>')
def index(plan_id=None):
    return render_template('index.html')

@app.route('/explore')
def explore():
    # ... (기존 explore 로직은 동일)
    return render_template('explore.html', plans=[])


@app.route('/get_plan/<plan_id>', methods=['GET'])
def get_plan(plan_id):
    # ... (기존 get_plan 로직은 동일)
    pass


@app.route('/generate', methods=['POST'])
def generate_plan():
    if not model or not db:
        return jsonify({'error': 'AI 모델 또는 데이터베이스가 초기화되지 않았습니다.'}), 500

    try:
        data = request.json
        # AI에게 전달할 프롬프트를 별도로 구성합니다.
        original_prompt = f"""
        당신은 사용자의 상세한 요구사항에 맞춰, 논리적으로 완벽하고 실용적인 다일차 여행 계획을 JSON 형식으로 생성하는 최고의 전문 여행 플래너입니다.
        당신의 가장 중요한 임무는 **일자별 동선의 논리적 연속성**과 **현실적인 식사 계획**을 보장하는 것입니다.

        **[절대 규칙 1: 숙소 연속성]**
        1.  **첫째 날**: 일정의 **맨 마지막** 활동은 반드시 `type: '숙소'` 이어야 합니다.
        2.  **중간 날짜 (둘째 날부터 마지막 전날까지)**:
            * 일정의 **맨 처음** 활동은 **반드시 이전 날 마지막에 머물렀던 '숙소'와 동일한 장소**여야 합니다.
            * 일정의 **맨 마지막** 활동은 반드시 `type: '숙소'` 이어야 합니다.
        3.  **마지막 날**: 일정의 **맨 처음** 활동은 **반드시 이전 날 마지막에 머물렀던 '숙소'와 동일한 장소**여야 합니다.

        **[절대 규칙 2: 식사 계획]**
        1.  **중간 날짜 (첫날과 마지막 날을 제외한 모든 날)**: 반드시 아침, 점심, 저녁 식사를 포함하여 하루에 총 3개의 '식사' 활동을 추천해야 합니다.
        2.  **첫째 날 식사**: 사용자의 도착 시간('{data.get('arrivalTime')}')을 기준으로 계획하세요.
        3.  **마지막 날 식사**: 사용자의 출발 시간('{data.get('departureTime')}')을 기준으로 계획하세요.

        **[절대 규칙 3: 장소의 정확성]**
        1. 모든 장소는 반드시 요청된 여행지 '{data.get('destination')}' 내에 있어야 합니다. 다른 국가나 도시의 장소를 절대로 포함해서는 안 됩니다.
        2. 'place' 값은 지도에서 검색 가능한 **실제 가게의 정확한 상호명**이어야 합니다.

        **[사용자 여행 조건]**
        * **여행지**: {data.get('destination')}
        * **기간**: {data.get('duration')}
        * **동행**: {data.get('companions')}
        * **여행 속도**: {data.get('pace')}
        * **선호 활동**: {', '.join(data.get('preferredActivities', []))}
        * **필수 방문지**: {data.get('mustVisit') or '없음'}
        * **기피 사항**: {data.get('toAvoid') or '없음'}
        * **이동 수단**: {data.get('transportation')}
        * **선호 숙소 유형**: {data.get('lodgingType')}
        * **첫날 도착 시간**: {data.get('arrivalTime')}
        * **마지막 날 출발 시간**: {data.get('departureTime')}
        
        **[출력 JSON 형식]**
        반드시 아래의 JSON 구조와 키 이름을 정확히 따르세요. `original_name` 필드에 현지 언어 이름을 반드시 포함해야 합니다.
        ```json
        {{
          "title": "AI가 추천하는 창의적인 여행 제목",
          "daily_plans": [
            {{
              "day": 1,
              "theme": "도시의 첫인상",
              "activities": [
                {{
                  "place": "세비야 대성당",
                  "original_name": "Catedral de Sevilla",
                  "description": "세계에서 세 번째로 큰 성당 방문",
                  "type": "관광"
                }}
              ]
            }}
          ]
        }}
        ```
        """
        
        MAX_RETRIES = 2
        validated_plan_str = None
        
        for i in range(MAX_RETRIES):
            print(f"AI 계획 생성 시도 #{i + 1}")
            response = model.generate_content(original_prompt)
            raw_text = response.text
            
            match = re.search(r'```json\s*(\{.*?\})\s*```', raw_text, re.DOTALL)
            if not match:
                match = re.search(r'\{.*\}', raw_text, re.DOTALL)
            if not match:
                original_prompt += "\n\n[오류 수정 요청] 이전 응답이 유효한 JSON 형식이 아닙니다. 반드시 JSON 형식으로만 응답해주세요."
                continue

            plan_json_str = match.group(1) if len(match.groups()) > 0 else match.group(0)
            plan_data = json.loads(plan_json_str)

            # 👇 [최종 수정] 서버 측에서 위치 유효성 검사
            destination_geocode = get_geocode(data.get('destination'))
            if not destination_geocode or not destination_geocode.get('viewport'):
                print("⚠️ 목적지 좌표를 찾을 수 없어 위치 검증을 건너뜁니다.")
                validated_plan_str = plan_json_str
                break

            destination_bounds = destination_geocode['viewport']
            is_valid = True
            invalid_places = []

            for day_plan in plan_data.get('daily_plans', []):
                for activity in day_plan.get('activities', []):
                    place_name = activity.get('original_name') or activity.get('place')
                    place_geocode = get_geocode(f"{place_name}, {data.get('destination')}")
                    
                    if not place_geocode:
                        is_valid = False
                        invalid_places.append(f"'{place_name}' (검색 실패)")
                        continue

                    lat = place_geocode['location']['lat']
                    lng = place_geocode['location']['lng']
                    
                    if not (destination_bounds['southwest']['lat'] <= lat <= destination_bounds['northeast']['lat'] and \
                            destination_bounds['southwest']['lng'] <= lng <= destination_bounds['northeast']['lng']):
                        is_valid = False
                        invalid_places.append(f"'{place_name}' (경계 벗어남)")
                
            if is_valid:
                print("✅ 계획 유효성 검증 성공!")
                validated_plan_str = plan_json_str
                break
            else:
                correction_request = f"\n\n[오류 수정 요청] 이전 계획에 '{data.get('destination')}'를 벗어나는 장소({', '.join(invalid_places)})가 포함되었습니다. 이 장소들을 '{data.get('destination')}' 내의 올바른 장소로 대체하여 계획 전체를 다시 생성해주세요."
                original_prompt += correction_request
                print(f"❌ 계획 유효성 검증 실패. 수정 요청: {correction_request}")

        if not validated_plan_str:
            raise ValueError("AI가 여러 번 시도했으나 올바른 계획을 생성하지 못했습니다.")

        final_plan_data = json.loads(validated_plan_str)
        
        full_plan_data = { 'plan': final_plan_data, 'request_details': data }
        doc_ref = db.collection('plans').document()
        doc_ref.set(full_plan_data)
        
        return jsonify({'plan': validated_plan_str, 'plan_id': doc_ref.id})
    
    except Exception as e:
        print(f"!!!!!!!!!! 플랜 생성 중 심각한 오류 발생 !!!!!!!!!!")
        print(f"오류 유형: {type(e).__name__}")
        print(f"오류 메시지: {e}")
        return jsonify({'error': '여행 계획 생성 중 서버 내부 오류가 발생했습니다.'}), 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
