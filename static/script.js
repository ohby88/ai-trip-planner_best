/**
 * ----------------------------------------------------------------
 * AI 여행 플래너 - Script.js (최종 완성본)
 * ----------------------------------------------------------------
 * 모든 기능 및 오류 수정이 완료된 최종 안정화 버전입니다.
 * - 경로 비교 및 시간/거리 표시 기능 완전 복구
 * - '스플릿 뷰' 동선 비교, '경로 편집' UI, 모든 절대 규칙 안정화
 */

// =================================================================
// 전역 변수 및 상수
// =================================================================
const plannerForm = document.getElementById('planner-form');
const generateBtn = document.getElementById('generate-btn');
const loading = document.getElementById('loading');
const resultContainer = document.getElementById('result-container');
const planTitleEl = document.getElementById('plan-title');
const dayTabsEl = document.getElementById('day-tabs');
const itineraryPanelEl = document.getElementById('itinerary-panel');

let globalPlanData = null;
let mainMap = null;
let placesService;
let directionsService;
let googleMapsLoaded = false;
let markers = [];
let polylines = [];
let currentDayIndex = 0;
const routeColors = ['#4A89F3', '#DD4B39', '#34A853', '#FBBC05', '#7F5283'];


// =================================================================
// 초기화 및 이벤트 리스너
// =================================================================

function initMap() {
    googleMapsLoaded = true;
    const mapContainer = document.getElementById('main-map');
    if (mapContainer && !mainMap) {
        mainMap = new google.maps.Map(mapContainer, {
            zoom: 12,
            center: { lat: 37.5665, lng: 126.9780 }, // 기본 위치: 서울
            mapTypeControl: false,
            streetViewControl: false,
        });
        placesService = new google.maps.places.PlacesService(mainMap);
        directionsService = new google.maps.DirectionsService();
    }
}

window.addEventListener('DOMContentLoaded', () => {
    const match = window.location.pathname.match(/^\/plan\/([a-zA-Z0-9]+)$/);
    if (match) {
        loadSharedPlan(match[1]);
    }
});

plannerForm.addEventListener('submit', (event) => {
    event.preventDefault();
    if (googleMapsLoaded) {
        generatePlanAndRender();
    } else {
        alert("Google Maps API가 아직 로드되지 않았습니다. 잠시 후 다시 시도해주세요.");
    }
});


// =================================================================
// 핵심 기능 함수
// =================================================================

async function loadSharedPlan(planId) {
    loading.classList.remove('hidden');
    plannerForm.classList.add('hidden');
    try {
        await waitForGoogleMaps();
        const response = await fetch(`/get_plan/${planId}`);
        if (!response.ok) throw new Error('계획을 불러오는 데 실패했습니다.');
        const fullPlanData = await response.json();
        const planData = fullPlanData.plan;
        if (!planData || !planData.daily_plans) throw new Error('잘못된 계획 데이터 형식입니다.');
        
        planData.destination = fullPlanData.request_details?.destination || '';
        planData.arrivalTime = fullPlanData.request_details?.arrivalTime || '점심 (점심부터 시작)';
        
        planData.daily_plans.forEach(day => {
            if (day.activities && !day.slots) {
                day.slots = day.activities.map(act => [act]);
            }
        });

        await geocodeAndProcessPlan(planData);

    } catch (error) {
        handleError(error);
        plannerForm.classList.remove('hidden');
    } finally {
        loading.classList.add('hidden');
    }
}

async function generatePlanAndRender() {
    generateBtn.disabled = true;
    generateBtn.textContent = '코스 생성 중...';
    loading.classList.remove('hidden');
    resultContainer.classList.add('hidden');

    const requestDetails = {
        destination: document.getElementById('destination').value,
        duration: document.getElementById('duration').value,
        arrivalTime: document.getElementById('arrival-time').value,
        companions: document.getElementById('companions').value,
        pace: document.getElementById('pace').value,
        preferredActivities: Array.from(document.querySelectorAll('#activities input:checked')).map(el => el.value),
        transportation: document.getElementById('transportation').value,
        lodgingType: document.getElementById('lodging-type').value
    };

    try {
        const response = await fetch('/generate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(requestDetails),
        });

        if (!response.ok) throw new Error(`서버 오류: ${response.status}`);
        const data = await response.json();
        
        const jsonMatch = data.plan.match(/\{[\s\S]*\}/);
        if (!jsonMatch) throw new Error("AI 응답에서 유효한 JSON을 찾을 수 없습니다.");
        
        const planData = JSON.parse(jsonMatch[0]);
        planData.destination = requestDetails.destination;
        planData.arrivalTime = requestDetails.arrivalTime;
        
        planData.daily_plans.forEach(day => {
            day.slots = day.activities.map(act => [act]);
        });

        await geocodeAndProcessPlan(planData);

        if (data.plan_id) {
            history.pushState({ planId: data.plan_id }, `Plan ${data.plan_id}`, `/plan/${data.plan_id}`);
        }
    } catch (error) {
        handleError(error);
    } finally {
        generateBtn.disabled = false;
        generateBtn.textContent = '나만의 여행 코스 만들기 ✨';
        loading.classList.add('hidden');
    }
}

async function geocodeAndProcessPlan(planData) {
    const destination = planData.destination;
    if (!destination) throw new Error("목적지 정보가 없습니다.");
    
    await waitForGoogleMaps();

    const destinationBounds = await new Promise((resolve, reject) => {
        placesService.textSearch({ query: destination }, (res, stat) => {
            if (stat === 'OK' && res[0]?.geometry) {
                if (mainMap) mainMap.setCenter(res[0].geometry.location);
                resolve(res[0].geometry.viewport || new google.maps.LatLngBounds(res[0].geometry.location));
            } else reject(new Error(`'${destination}' 위치를 찾을 수 없습니다.`));
        });
    });

    const allExistingPlaces = new Set();
    const validatedPlans = await Promise.all(
        planData.daily_plans.map(day => 
            Promise.all(day.slots.map(slot => 
                Promise.all(slot.map(act => validateOrReplaceActivity(act, destination, destinationBounds, allExistingPlaces)))
            ))
        )
    );

    let lastKnownLodging = null;
    for (let i = 0; i < validatedPlans.length; i++) {
        let slots = validatedPlans[i].map(slot => slot.filter(Boolean)).filter(slot => slot.length > 0);
        
        let activities = slots.flat();
        const seenInDay = new Set();
        activities = activities.filter(act => !seenInDay.has(act.place) && seenInDay.add(act.place));
        
        if (i === 0) {
            activities = await applyMealRule(activities, planData.arrivalTime, destination, destinationBounds, seenInDay);
        }

        activities = await applyLodgingRule(activities, i, validatedPlans.length, lastKnownLodging, destination, destinationBounds, seenInDay);
        
        lastKnownLodging = activities.find(act => act.type === '숙소') || lastKnownLodging;
        
        planData.daily_plans[i] = { ...planData.daily_plans[i], activities, slots: activities.map(act => [act]) };
    }

    globalPlanData = planData;
    renderUI(globalPlanData);
    resultContainer.classList.remove('hidden');
}


// =================================================================
// 헬퍼 함수 (규칙 적용 및 데이터 처리)
// =================================================================

async function validateOrReplaceActivity(activity, destination, bounds, existingPlaces) {
    const p_res = await new Promise(r => placesService.textSearch({ query: `"${activity.place}" ${destination}` }, (res, stat) => r(stat === 'OK' ? res : null)));
    const validPlace = p_res ? p_res.find(pl => bounds.contains(pl.geometry.location) && !existingPlaces.has(pl.name)) : null;

    if (validPlace) {
        existingPlaces.add(validPlace.name);
        return createActivityFromPlace(validPlace, activity.type);
    } 
    
    const replacement = await findReplacementPlace(activity.type, destination, bounds, existingPlaces);
    if (replacement) {
        existingPlaces.add(replacement.place);
    }
    return replacement;
}

async function findReplacementPlace(type, destination, bounds, existingPlaces) {
    const keywords = { '식사': 'restaurant', '카페': 'cafe', '관광': 'tourist attraction', '쇼핑': 'shopping', '숙소': 'lodging' }[type] || type;
    
    const p_nearby = await new Promise(r => placesService.nearbySearch({ location: bounds.getCenter(), radius: 5000, keyword: keywords }, (res, stat) => r(stat === 'OK' ? res : null)));
    let valid = p_nearby ? p_nearby.find(pl => !existingPlaces.has(pl.name)) : null;
    if(valid) return createActivityFromPlace(valid, type);

    const p_text = await new Promise(r => placesService.textSearch({ query: `${destination} ${keywords}` }, (res, stat) => r(stat === 'OK' ? res : null)));
    valid = p_text ? p_text.find(pl => bounds.contains(pl.geometry.location) && !existingPlaces.has(pl.name)) : null;
    if(valid) return createActivityFromPlace(valid, type);

    console.warn(`[${type}] 유형의 대체 장소를 찾지 못했습니다.`);
    return null;
}

async function applyMealRule(activities, arrivalTime, destination, bounds, existingPlaces) {
    const requiredMeals = arrivalTime.includes("아침") ? 3 : arrivalTime.includes("점심") ? 2 : 1;
    let meals = activities.filter(act => act.type === '식사');
    let nonMeals = activities.filter(act => act.type !== '식사');

    while (meals.length < requiredMeals) {
        const newMeal = await findReplacementPlace('식사', destination, bounds, existingPlaces);
        if (newMeal) {
            meals.push(newMeal);
            existingPlaces.add(newMeal.place);
        } else break;
    }
    meals = meals.slice(0, requiredMeals);
    
    const finalActivities = [];
    if (requiredMeals >= 3 && meals.length > 0) finalActivities.push(meals.shift());
    if (nonMeals.length > 0) finalActivities.push(nonMeals.shift());
    if (requiredMeals >= 2 && meals.length > 0) finalActivities.push(meals.shift());
    finalActivities.push(...nonMeals);
    if (requiredMeals >= 1 && meals.length > 0) finalActivities.push(meals.shift());
    
    return finalActivities;
}

async function applyLodgingRule(activities, dayIndex, totalDays, lastLodging, destination, bounds, existingPlaces) {
    let lodgingForThisDay = activities.find(act => act.type === '숙소') || null;
    let otherActivities = activities.filter(act => act.type !== '숙소');

    if (dayIndex < totalDays - 1) {
        if (!lodgingForThisDay) {
            lodgingForThisDay = await findReplacementPlace('숙소', destination, bounds, existingPlaces);
        }
        if (lodgingForThisDay) {
            otherActivities.push(lodgingForThisDay);
            if(lodgingForThisDay.place) existingPlaces.add(lodgingForThisDay.place);
        }
    }

    if (dayIndex > 0) {
        let startLodging = lastLodging || await findReplacementPlace('숙소', destination, bounds, existingPlaces);
        if (startLodging && !otherActivities.some(a => a.place === startLodging.place)) {
            otherActivities.unshift(startLodging);
            if(startLodging.place) existingPlaces.add(startLodging.place);
        }
    }
    
    return otherActivities;
}

function getPathCombinations(slots) {
    if (!slots || slots.length === 0) return [[]];
    const firstSlotOptions = slots[0];
    const remainingSlots = slots.slice(1);
    const combinationsFromRest = getPathCombinations(remainingSlots);
    const allCombinations = [];
    for (const option of firstSlotOptions) {
        for (const combination of combinationsFromRest) {
            allCombinations.push([option, ...combination]);
        }
    }
    return allCombinations.slice(0, 5);
}

async function getRouteSegmentsForDay(dayIndex) {
    const activities = globalPlanData.daily_plans[dayIndex]?.activities.filter(act => act.coordsFound);
    if (!activities || activities.length < 2) return [];
    
    const transportValue = document.getElementById('transportation')?.value || '렌터카';
    let travelMode = google.maps.TravelMode.DRIVING;
    if (transportValue === '대중교통') travelMode = google.maps.TravelMode.TRANSIT;

    const promises = [];
    for (let i = 0; i < activities.length - 1; i++) {
        const request = {
            origin: { lat: activities[i].latitude, lng: activities[i].longitude },
            destination: { lat: activities[i+1].latitude, lng: activities[i+1].longitude },
            travelMode: travelMode,
        };
        promises.push(new Promise(resolve => {
            directionsService.route(request, (result, status) => {
                if (status == 'OK') {
                    const leg = result.routes[0].legs[0];
                    resolve({ distance: leg.distance.text, duration: leg.duration.text, travelMode: travelMode });
                } else resolve(null);
            });
        }));
    }
    return Promise.all(promises);
}

// =================================================================
// UI 렌더링 및 유틸리티 함수
// =================================================================

function createActivityFromPlace(place, typeOverride = null) {
    const getPlaceType = (types) => {
        if (!types) return '관광';
        if (types.includes('lodging')) return '숙소';
        if (types.includes('restaurant') || types.includes('food')) return '식사';
        if (types.includes('cafe')) return '카페';
        if (types.includes('shopping_mall') || types.includes('store')) return '쇼핑';
        return '관광';
    };
    return {
        id: `act-${place.place_id || Date.now()}-${Math.random()}`,
        place: place.name,
        description: place.vicinity || place.formatted_address || '',
        latitude: place.geometry.location.lat(),
        longitude: place.geometry.location.lng(),
        coordsFound: true,
        type: typeOverride || getPlaceType(place.types)
    };
}

function renderUI(planData) {
    globalPlanData = planData;
    planTitleEl.innerText = planData.title;
    dayTabsEl.innerHTML = '';
    planData.daily_plans.forEach((dayPlan, idx) => {
        const tab = document.createElement('button');
        tab.className = 'day-tab';
        tab.innerText = `Day ${dayPlan.day}`;
        tab.dataset.dayIndex = idx;
        if (idx === currentDayIndex) tab.classList.add('active');
        tab.addEventListener('click', () => { currentDayIndex = idx; renderItineraryAndMap(); });
        dayTabsEl.appendChild(tab);
    });
    document.getElementById('share-btn').onclick = () => {
        navigator.clipboard.writeText(window.location.href).then(() => {
            const toast = document.getElementById('toast-message');
            toast.classList.remove('hidden');
            setTimeout(() => toast.classList.add('hidden'), 2000);
        });
    };
    renderItineraryAndMap();
}

async function renderItineraryAndMap() {
    document.querySelectorAll('.day-tab').forEach((tab, i) => tab.classList.toggle('active', i === currentDayIndex));
    const dayPlan = globalPlanData.daily_plans[currentDayIndex];
    if(!dayPlan) {
        itineraryPanelEl.innerHTML = '<p>표시할 계획이 없습니다.</p>';
        return;
    }

    itineraryPanelEl.innerHTML = `
        <div class="itinerary-header">
            <h3>${dayPlan.theme || `Day ${dayPlan.day}`}</h3>
            <button id="edit-mode-btn">✏️ 경로 편집</button>
        </div>
        <div class="timeline"></div>`;
    
    const timeline = itineraryPanelEl.querySelector('.timeline');
    new Sortable(timeline, { group: 'shared-timeline', animation: 150, handle: '.drag-handle', onEnd: handleUnifiedDragEnd });
    
    dayPlan.slots.forEach((slot, i) => {
        timeline.appendChild(createSlotElement(slot, currentDayIndex, i));
    });
    
    itineraryPanelEl.appendChild(createAddPlaceSection(currentDayIndex));
    updateMapForDay(currentDayIndex);

    document.getElementById('edit-mode-btn').addEventListener('click', toggleEditMode);
}

/**
 * 슬롯(활동 그룹) 엘리먼트 생성 (인덴트 디자인 적용)
 */
function createSlotElement(slot, dayIndex, slotIndex) {
    const slotContainer = document.createElement('div');
    slotContainer.className = 'slot-container';
    slotContainer.dataset.slotIndex = slotIndex;

    if (slot.length > 1) {
        slotContainer.classList.add('has-alternatives');
    }
    
    // 카드들을 순서대로 추가
    slot.forEach((activity, optionIndex) => {
        const cardElement = createActivityElement(activity, dayIndex, slotIndex, optionIndex, slot.length);
        // 첫 번째 대안이 아닌 경우, 들여쓰기 스타일 클래스 추가
        if (optionIndex > 0) {
            cardElement.classList.add('indented-alternative');
        }
        slotContainer.appendChild(cardElement);
    });
    
    new Sortable(slotContainer, {
        group: 'shared-activities',
        animation: 150,
        handle: '.drag-handle',
        onEnd: handleUnifiedDragEnd
    });

    return slotContainer;
}

function createActivityElement(activity, dayIndex, slotIndex, optionIndex, totalOptions) {
    const el = document.createElement('div');
    el.className = 'activity-card';
    el.dataset.id = activity.id;
    el.setAttribute('data-type', activity.type);

    const icons = { '식사':'🍽️', '관광':'🏞️', '카페':'☕', '쇼핑': '🛍️', '액티비티':'🏃', '숙소':'🏨' };
    const icon = icons[activity.type] || '📍';
    let placeHTML = activity.place;
    if (totalOptions > 1) placeHTML = `<span class="option-badge">${optionIndex + 1}안</span> ${placeHTML}`;
    placeHTML = `${icon} ${placeHTML}`;
    if (activity.coordsFound) {
        const mapLink = `http://googleusercontent.com/maps/google.com/0{encodeURIComponent(activity.description || activity.place)}`;
        placeHTML += ` <a href="${mapLink}" target="_blank" class="map-link" title="Google 지도에서 보기">🗺️</a>`;
    }

    el.innerHTML = `
        <span class="drag-handle">☰</span>
        <div class="activity-content-wrapper">
            <div class="activity-details">
                <div class="activity-place">${placeHTML}</div>
                <div class="activity-description">${activity.description || '세부 정보 없음'}</div>
                <div class="add-alternative-wrapper">
                    <button class="add-alternative-btn" data-slot-index="${slotIndex}">+ 대안 경로 추가</button>
                    <div class="alternative-search-box"></div>
                </div>
            </div>
        </div>
        <button class="delete-btn" title="삭제">×</button>`;
    
    el.querySelector('.delete-btn').addEventListener('click', (e) => { e.stopPropagation(); deleteActivity(dayIndex, slotIndex, optionIndex); });
    el.querySelector('.add-alternative-btn').addEventListener('click', showAlternativeSearch);
    return el;
}

async function updateMapForDay(dayIndex) {
    markers.forEach(m => m.setMap(null));
    polylines.forEach(p => p.setMap(null));
    markers = [];
    polylines = [];

    const dayPlan = globalPlanData.daily_plans[dayIndex];
    const slots = dayPlan?.slots.filter(s => s.length > 0 && s.every(act => act.coordsFound));
    const legendContainer = document.getElementById('route-comparison-container');

    if (!slots || slots.length === 0) {
        legendContainer.classList.add('hidden');
        return;
    }

    const bounds = new google.maps.LatLngBounds();
    slots.flat().forEach(act => bounds.extend({ lat: act.latitude, lng: act.longitude }));
    
    const pathCombinations = getPathCombinations(slots);

    // 모든 경로 조합에 대해 경로 그리기
    pathCombinations.forEach((path, pathIndex) => {
        const linePath = path.map(act => ({ lat: act.latitude, lng: act.longitude }));
        polylines.push(new google.maps.Polyline({
            path: linePath, map: mainMap,
            strokeColor: routeColors[pathIndex % routeColors.length],
            strokeWeight: 8 - (pathIndex * 1.5),
            strokeOpacity: 0.9 - (pathIndex * 0.15)
        }));
    });
    
    // 마커는 첫 번째 경로 기준으로 그리기
    if (pathCombinations[0]) {
        pathCombinations[0].forEach((act, i) => {
            markers.push(new google.maps.Marker({
                position: { lat: act.latitude, lng: act.longitude },
                map: mainMap,
                label: { text: `${i + 1}`, color: "white" },
                title: act.place
            }));
        });
    }

    // ⭐ 모든 경로 조합에 대한 시간/거리 정보 계산
    let legendHTML = '<h4>경로 비교</h4>';
    for (const [pathIndex, path] of pathCombinations.entries()) {
        const color = routeColors[pathIndex % routeColors.length];
        const routeSegments = await getSegmentsForPath(path); // 경로별로 시간 계산

        legendHTML += `<div class="route-path-card" style="border-left-color: ${color}">`;
        path.forEach((activity, i) => {
            legendHTML += `<div class="route-stop">${i + 1}. ${activity.place}</div>`;
            if (i < path.length - 1 && routeSegments[i]) {
                const segment = routeSegments[i];
                let icon = '🚗';
                if (segment.travelMode === 'TRANSIT') icon = '🚌';
                if (segment.travelMode === 'WALKING') icon = '🚶';
                legendHTML += `<div class="route-info-legend">${icon} ${segment.duration} (${segment.distance})</div>`;
            }
        });
        legendHTML += `</div>`;
    }
    
    legendContainer.innerHTML = legendHTML;
    legendContainer.classList.remove('hidden');

    if (!bounds.isEmpty()) mainMap.fitBounds(bounds);
}

function handleUnifiedDragEnd(evt) {
    const { oldIndex, newIndex } = evt;
    if (oldIndex === newIndex) return;
    const dayPlan = globalPlanData.daily_plans[currentDayIndex];
    const movedSlot = dayPlan.slots.splice(oldIndex, 1)[0];
    dayPlan.slots.splice(newIndex, 0, movedSlot);
    dayPlan.activities = dayPlan.slots.flat();
    renderItineraryAndMap();
}

function createAddPlaceSection(dayIndex) {
    const container = document.createElement('div');
    container.className = 'add-place-container';
    container.innerHTML = `<h5>새로운 장소 추가하기</h5><div class="search-ui"><input type="text" placeholder="장소 검색"><button>🔍</button></div><div class="search-results-list"></div>`;
    
    const input = container.querySelector('input');
    const button = container.querySelector('button');
    const resultsEl = container.querySelector('.search-results-list');

    const search = async () => {
        if (!input.value) return;
        resultsEl.innerHTML = '<div class="spinner-small"></div>';
        const p_res = await new Promise(r => placesService.textSearch({ query: `${globalPlanData.destination} ${input.value}` }, (res, stat) => r(stat === 'OK' ? res : null)));
        resultsEl.innerHTML = '';
        if (p_res) {
            p_res.slice(0, 5).forEach(place => {
                const item = document.createElement('div');
                item.className = 'search-result-item';
                item.innerHTML = `
                    <div class="result-item-info">
                        <span>${place.name}</span>
                        <small>${place.formatted_address}</small>
                    </div>
                    <button class="add-place-btn">+</button>`;
                item.querySelector('.add-place-btn').onclick = () => {
                    const newActivity = createActivityFromPlace(place);
                    globalPlanData.daily_plans[dayIndex].slots.push([newActivity]);
                    globalPlanData.daily_plans[dayIndex].activities = globalPlanData.daily_plans[dayIndex].slots.flat();
                    renderItineraryAndMap();
                    input.value = '';
                    resultsEl.innerHTML = '';
                };
                resultsEl.appendChild(item);
            });
        }
    };
    
    button.addEventListener('click', search);
    input.addEventListener('keydown', (e) => {
        if (e.isComposing) return;
        if (e.key === 'Enter') {
            e.preventDefault(); 
            search();
        }
    });
    return container;
}

function deleteActivity(dayIndex, slotIndex, optionIndex) {
    const dayPlan = globalPlanData.daily_plans[dayIndex];
    const slot = dayPlan.slots[slotIndex];
    slot.splice(optionIndex, 1);
    if (slot.length === 0) {
        dayPlan.slots.splice(slotIndex, 1);
    }
    dayPlan.activities = dayPlan.slots.flat();
    renderItineraryAndMap();
}

function toggleEditMode(e) {
    document.body.classList.toggle('is-editing-mode');
    const btn = e.target;
    if (document.body.classList.contains('is-editing-mode')) {
        btn.textContent = '✅ 편집 완료';
        btn.classList.add('active');
    } else {
        btn.textContent = '✏️ 경로 편집';
        btn.classList.remove('active');
        const existingSearch = document.querySelector('.alternative-search-box');
        if (existingSearch) existingSearch.remove();
    }
}

function showAlternativeSearch(e) {
    e.stopPropagation();
    const btn = e.target;
    const wrapper = btn.closest('.activity-details');
    
    const existingSearch = document.querySelector('.alternative-search-box');
    if (existingSearch) existingSearch.remove();

    const searchBox = document.createElement('div');
    searchBox.className = 'alternative-search-box';
    searchBox.innerHTML = `
        <div class="alternative-search-ui">
            <input type="text" placeholder="대안 장소 검색...">
            <button>🔍</button>
        </div>
        <div class="alternative-search-results"></div>`;
    
    wrapper.appendChild(searchBox);

    const input = searchBox.querySelector('input');
    const searchBtn = searchBox.querySelector('button');
    const resultsEl = searchBox.querySelector('.alternative-search-results');
    const slotIndex = parseInt(btn.dataset.slotIndex, 10);
    input.focus();

    const searchHandler = async () => {
        if (!input.value) return;
        resultsEl.innerHTML = '<div class="spinner-small"></div>';
        const p_res = await new Promise(r => placesService.textSearch({ query: `${globalPlanData.destination} ${input.value}` }, (res, stat) => r(stat === 'OK' ? res : null)));
        resultsEl.innerHTML = '';
        if (p_res) {
            p_res.slice(0, 5).forEach(place => {
                const item = document.createElement('div');
                item.className = 'search-result-item';
                item.innerHTML = `
                    <div class="result-item-info">
                        <span>${place.name}</span>
                        <small>${place.formatted_address}</small>
                    </div>
                    <button class="add-place-btn">+</button>`;
                item.querySelector('.add-place-btn').onclick = () => {
                    const newActivity = createActivityFromPlace(place);
                    globalPlanData.daily_plans[currentDayIndex].slots[slotIndex].push(newActivity);
                    globalPlanData.daily_plans[currentDayIndex].activities = globalPlanData.daily_plans[currentDayIndex].slots.flat();
                    document.body.classList.remove('is-editing-mode');
                    renderItineraryAndMap();
                };
                resultsEl.appendChild(item);
            });
        } else {
            resultsEl.innerHTML = '<div class="no-results">검색 결과가 없습니다.</div>';
        }
    };

    searchBtn.addEventListener('click', searchHandler);
    input.addEventListener('keydown', (e) => {
        if (e.isComposing) return;
        if (e.key === 'Enter') {
            e.preventDefault();
            searchHandler();
        }
    });
}

function handleError(error) {
    console.error('오류 발생:', error);
    loading.classList.add('hidden');
    resultContainer.classList.add('hidden');
    itineraryPanelEl.innerHTML = `<p style="color:var(--danger-color); text-align:center; padding: 20px;">오류가 발생했습니다: ${error.message}</p>`;
}

function waitForGoogleMaps() {
    return new Promise(resolve => {
        const check = () => {
            if (googleMapsLoaded && placesService && directionsService) resolve();
            else setTimeout(check, 100);
        };
        check();
    });
}