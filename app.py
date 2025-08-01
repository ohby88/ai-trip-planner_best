import os
import json
import re
import requests
from flask import Flask, request, jsonify, render_template
from flask_cors import CORS
import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv

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
    db = None
except Exception as e:
    print(f"❌ Firebase 초기화 중 예측하지 못한 오류 발생: {e}")
    db = None


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

# AI 호출 로직을 클라이언트로 옮겼기 때문에, 서버는 이제 Firestore에 저장하는 역할만 담당합니다.
@app.route('/save_plan', methods=['POST'])
def save_plan():
    if not db:
        return jsonify({'error': '데이터베이스가 초기화되지 않았습니다.'}), 500
    try:
        data = request.json
        final_plan_data = data.get('plan')
        request_details = data.get('request_details')

        if not final_plan_data or not request_details:
            raise ValueError("요청 데이터 형식이 올바르지 않습니다.")

        full_plan_data = { 'plan': final_plan_data, 'request_details': request_details }
        doc_ref = db.collection('plans').document()
        doc_ref.set(full_plan_data)
        
        return jsonify({'plan_id': doc_ref.id})
    except Exception as e:
        print(f"플랜 저장 중 오류: {e}")
        return jsonify({'error': str(e)}), 500

# 로컬 개발 환경에서 직접 실행할 때 사용
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
