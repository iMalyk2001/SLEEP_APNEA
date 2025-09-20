/* globals Chart */
(function(){
	const state = {
		token: null,
		username: null,
		deviceKey: null,
		ws: null,
		connected: false,
		logEnabled: true,
		windowSeconds: 10,
		lastSecondCount: 0,
		samplesThisSecond: 0,
		lastSecondTs: 0,
		buffers: {
			labels: [],
			s1: [],
			s2: [],
			s3: [],
		},
		chart: null,
		chart2: null,
		chart3: null,
		bpm: null,
		lastBpm: null,
		bpmSeries: [], // smoothed BPM values used for chart
		bpmRawSeries: [], // raw BPM values before smoothing
		bpmSmoothingWindow: 10, // number of recent BPM values for rolling average
		updatePending: false,
		alerts: [],
		apneaWindowSec: 10,
		apneaMvThreshold: 5,
		srThreshold: 100,
		srAutoInitDone: false,
		alertSound: true,
		bpmBelowStartMs: 0,
		bpmAlarmActive: false,
		lastDisconnectAlertAt: 0,
	};

	const els = {
		username: document.getElementById('username'),
		password: document.getElementById('password'),
		btnLogin: document.getElementById('btn-login'),
		btnRegister: document.getElementById('btn-register'),
		btnLogout: document.getElementById('btn-logout'),
		authForms: document.getElementById('auth-forms'),
		authSession: document.getElementById('auth-session'),
		userLabel: document.getElementById('user-label'),
		connStatus: document.getElementById('conn-status'),
		sampleRate: document.getElementById('sample-rate'),
		deviceKey: document.getElementById('device-key'),
		btnExport: document.getElementById('btn-export'),
		btnTest: document.getElementById('btn-test'),
		toggleLog: document.getElementById('toggle-log'),
		windowSeconds: document.getElementById('window-seconds'),
		c1: document.getElementById('chart1'),
		c2: document.getElementById('chart2'),
		c3: document.getElementById('chart3'),
		alertList: document.getElementById('alert-list'),
		apneaWindow: document.getElementById('apnea-window'),
		apneaThreshold: document.getElementById('apnea-threshold'),
		srThresholdLabel: document.getElementById('sr-threshold-label'),
		srSlider: document.getElementById('sr-slider'),
		srApply: document.getElementById('sr-apply'),
		alertSound: document.getElementById('alert-sound'),
		bpmLabel: document.getElementById('bpm-label'),
	};

	// IndexedDB per-user
	let db;
	function openDB(username){
		return new Promise((resolve, reject)=>{
			const req = indexedDB.open('breathDB_' + username, 1);
			req.onupgradeneeded = (e)=>{
				const d = e.target.result;
				if(!d.objectStoreNames.contains('samples')){
					const store = d.createObjectStore('samples', { keyPath: 'timestamp' });
					store.createIndex('ts', 'timestamp', { unique: true });
				}
			};
			req.onsuccess = ()=>{ db = req.result; resolve(); };
			req.onerror = ()=>reject(req.error);
		});
	}
	function addSampleToDB(sample){
		if(!db || !state.logEnabled) return;
		const tx = db.transaction('samples', 'readwrite');
		tx.objectStore('samples').put(sample);
	}
	async function exportCSV(){
		if(!db){ alert('No data'); return; }
		const tx = db.transaction('samples', 'readonly');
		const store = tx.objectStore('samples');
		const req = store.openCursor();
		const rows = [['timestamp','sensor1_mV','sensor2_mV','sensor3_mV']];
		await new Promise((resolve)=>{
			req.onsuccess = (e)=>{
				const cursor = e.target.result;
				if(cursor){
					const s = cursor.value;
					rows.push([s.timestamp, s.sensor1 ?? '', s.sensor2 ?? '', s.sensor3 ?? '']);
					cursor.continue();
				}else{
					resolve();
				}
			};
		});
		const csv = rows.map(r=>r.join(',')).join('\n');
		const blob = new Blob([csv], { type: 'text/csv' });
		const a = document.createElement('a');
		a.href = URL.createObjectURL(blob);
		a.download = `breathing_${state.username}_${Date.now()}.csv`;
		a.click();
	}

	function setConnected(v){
		const now = Date.now();
		if(state.connected && !v){
			if(now - state.lastDisconnectAlertAt > 5000){
				pushAlert('disconnect', 'Connection lost');
				state.lastDisconnectAlertAt = now;
			}
		}
		state.connected = v;
		els.connStatus.textContent = v ? 'Online' : 'Offline';
		els.connStatus.classList.toggle('online', v);
		els.connStatus.classList.toggle('offline', !v);
	}

	function scheduleChartUpdate(){
		if(state.updatePending) return;
		state.updatePending = true;
		requestAnimationFrame(()=>{
			try{ state.chart.update('none'); }catch(e){}
			try{ state.chart2.update('none'); }catch(e){}
			state.updatePending = false;
		});
	}

	function initCharts(){
		const common = {
			animation: false,
			responsive: true,
			maintainAspectRatio: false,
			scales: {
				x: { display: false },
				y: { title: { display: true, text: 'mV' }, grid: { color: 'rgba(255,255,255,0.06)' }, beginAtZero: false, suggestedMin: -50, suggestedMax: 50 },
			},
			plugins: { legend: { display: false } },
		};
		// Top chart: BPM only (0-150)
		state.chart = new Chart(els.c1.getContext('2d'), {
			type: 'line',
			data: { labels: state.buffers.labels, datasets: [
				{ data: state.bpmSeries, borderColor: '#f0c000', borderWidth: 2, pointRadius: 0 },
			]},
			options: {
				animation: false,
				responsive: true,
				maintainAspectRatio: false,
				scales: {
					x: { display: false },
					y: { title: { display: true, text: 'BPM' }, min: 0, max: 150, grid: { color: 'rgba(255,255,255,0.06)' } },
				},
				plugins: { legend: { display: false } },
			},
		});
		state.chart2 = new Chart(els.c2.getContext('2d'), {
			type: 'line',
			// Bottom chart: combined mV waveforms (sensor1 blue, sensor2 green)
			data: { labels: state.buffers.labels, datasets: [
				{ data: state.buffers.s1, borderColor: '#2f81f7', borderWidth: 1, pointRadius: 0 },
				{ data: state.buffers.s2, borderColor: '#2ea043', borderWidth: 1, pointRadius: 0 },
			] },
			options: common,
		});
		// Removed third chart
	}

	function trimWindow(){
		const maxPoints = Math.max(10, state.windowSeconds * 1000);
		const trim = (arr)=>{
			if(arr.length > maxPoints){
				arr.splice(0, arr.length - maxPoints);
			}
		};
		trim(state.buffers.labels);
		trim(state.buffers.s1);
		trim(state.buffers.s2);
		trim(state.buffers.s3);
		trim(state.bpmSeries);
		// Keep BPM buffers bounded as well (use smaller bound based on smoothing window)
		const bpmMax = Math.max(state.bpmSmoothingWindow * 4, 120);
		if(state.bpmRawSeries.length > bpmMax){ state.bpmRawSeries.splice(0, state.bpmRawSeries.length - bpmMax); }
	}

	function onSample(s){
		state.buffers.labels.push('');
		state.buffers.s1.push(s.sensor1 ?? null);
		state.buffers.s2.push(s.sensor2 ?? null);
		state.buffers.s3.push(s.sensor3 ?? null);
		trimWindow();
		if(state.logEnabled) addSampleToDB(s);
		if(!state.lastSecondTs) state.lastSecondTs = s.timestamp;
		if(Math.floor(s.timestamp/1000) === Math.floor(state.lastSecondTs/1000)){
			state.samplesThisSecond++;
		}else{
			state.lastSecondCount = state.samplesThisSecond;
			state.samplesThisSecond = 1;
			state.lastSecondTs = s.timestamp;
			els.sampleRate.textContent = String(state.lastSecondCount);
			checkLowSampleRate();
			// Auto-initialize low SR threshold to first measured rate once
			if(!state.srAutoInitDone && state.lastSecondCount > 0){
				state.srThreshold = state.lastSecondCount;
				if(els.srSlider){ els.srSlider.value = String(state.srThreshold); }
				if(els.srThresholdLabel){ els.srThresholdLabel.textContent = String(state.srThreshold); }
				state.srAutoInitDone = true;
			}
		}
		// Server BPM carry-forward: add lastBpm so the line remains continuous
		const carry = (typeof state.lastBpm === 'number' && isFinite(state.lastBpm)) ? state.lastBpm : null;
		state.bpmSeries.push(carry);
		scheduleChartUpdate();
		checkApnea();
	}

	function computeBpm(samples, sampleRateHz){
		const fs = sampleRateHz;
		if(!samples || samples.length < Math.floor(5 * fs)) return null;
		// 1) Detrend (~0.5 s)
		const w = Math.max(1, Math.floor(0.5 * fs));
		const detr = [];
		let acc = 0;
		for(let i=0;i<samples.length;i++){
			acc += samples[i];
			if(i >= w) acc -= samples[i - w];
			detr.push(samples[i] - acc / Math.min(i + 1, w));
		}
		// 2) Rectify + smooth (~0.3 s)
		const rect = detr.map(x=>Math.abs(x));
		const w2 = Math.max(1, Math.floor(0.3 * fs));
		const sm = [];
		acc = 0;
		for(let i=0;i<rect.length;i++){
			acc += rect[i];
			if(i >= w2) acc -= rect[i - w2];
			sm.push(acc / Math.min(i + 1, w2));
		}
		// 3) Threshold + peak detection (min 0.8 s)
		const mean = sm.reduce((a,b)=>a+b,0) / sm.length;
		const std = Math.sqrt(sm.reduce((a,x)=>a+(x-mean)**2,0) / sm.length);
		const thr = mean + 0.5 * std;
		const minDist = Math.floor(0.8 * fs);
		const peaks = [];
		let last = -minDist;
		for(let i=1;i<sm.length-1;i++){
			if(sm[i] > thr && sm[i] > sm[i-1] && sm[i] >= sm[i+1] && (i - last) >= minDist){
				peaks.push(i);
				last = i;
			}
		}
		if(peaks.length < 2) return null;
		const ibis = [];
		for(let i=1;i<peaks.length;i++) ibis.push((peaks[i]-peaks[i-1])/fs);
		const avgIbi = ibis.reduce((a,b)=>a+b,0) / ibis.length;
		return 60 / avgIbi;
	}

	function pushAlert(type, text){
		const ts = new Date();
		state.alerts.push({ type, text, ts });
		const li = document.createElement('li');
		li.className = `alert-item ${type}`;
		li.innerHTML = `<span>${text}</span><span class="time">${ts.toLocaleTimeString()}</span>`;
		els.alertList.prepend(li);
		if(els.alertList.children.length > 100){ els.alertList.removeChild(els.alertList.lastChild); }
		// Sound is managed by checkBpmAlarm() for BPM-low; other alerts are silent
	}

	let audioCtx; let beepGain;
	function initAudio(){
		try{
			audioCtx = new (window.AudioContext || window.webkitAudioContext)();
			beepGain = audioCtx.createGain();
			beepGain.connect(audioCtx.destination);
			beepGain.gain.value = 0.05;
		}catch(e){ /* ignore */ }
	}
	function beep(){
		if(!state.alertSound) return;
		if(!audioCtx) initAudio();
		if(!audioCtx) return;
		const o = audioCtx.createOscillator();
		o.type = 'sine'; o.frequency.value = 880;
		o.connect(beepGain);
		o.start();
		setTimeout(()=>{ try{ o.stop(); o.disconnect(); }catch(e){} }, 150);
	}

	// Continuous alarm (starts when BPM<60 for 10s, stops on recovery)
	let alarmOsc = null;
	let alarmGainNode = null;
	function alarmStartContinuous(){
		if(!state.alertSound) return;
		if(!audioCtx) initAudio();
		if(!audioCtx) return;
		if(alarmOsc) return; // already sounding
		alarmOsc = audioCtx.createOscillator();
		alarmGainNode = audioCtx.createGain();
		alarmOsc.type = 'square';
		alarmOsc.frequency.value = 1800;
		alarmGainNode.gain.value = 0.2;
		alarmOsc.connect(alarmGainNode);
		alarmGainNode.connect(beepGain);
		alarmOsc.start();
	}
	function alarmStopContinuous(){
		try{
			if(alarmOsc){ alarmOsc.stop(); alarmOsc.disconnect(); }
			if(alarmGainNode){ alarmGainNode.disconnect(); }
		}catch(e){}
		alarmOsc = null; alarmGainNode = null;
	}

	function rms(arr){
		let sum = 0, count = 0;
		for(let i=0;i<arr.length;i++){
			const v = arr[i];
			if(typeof v === 'number'){
				sum += v*v; count++;
			}
		}
		return count ? Math.sqrt(sum / count) : 0;
	}

	function checkApnea(){
		const N = Math.min(state.buffers.s1.length, state.apneaWindowSec * 1000);
		if(N < state.apneaWindowSec * 1000) return;
		const slice1 = state.buffers.s1.slice(-N);
		const slice2 = state.buffers.s2.slice(-N);
		const r1 = rms(slice1);
		const r2 = rms(slice2);
		const r = Math.max(r1, r2);
		if(r < state.apneaMvThreshold){
			const last = state.alerts[0];
			if(!last || !(last.type === 'apnea' && (Date.now() - last.ts.getTime() < 10000))){
				pushAlert('apnea', `Low motion: RMS ${r.toFixed(1)} mV (< ${state.apneaMvThreshold} mV)`);
			}
		}
	}

	function checkLowSampleRate(){
		if(state.lastSecondCount > 0 && state.lastSecondCount < state.srThreshold){
			// no beep for low-sr; informational only
			pushAlert('low-sr', `Low sample rate: ${state.lastSecondCount} Hz (< ${state.srThreshold} Hz)`);
		}
	}

	function checkBpmAlarm(currentBpm){
		const now = Date.now();
		if(typeof currentBpm === 'number' && currentBpm < 60){
			if(state.bpmBelowStartMs === 0){ state.bpmBelowStartMs = now; }
			const elapsed = now - state.bpmBelowStartMs;
			if(!state.bpmAlarmActive && elapsed >= 10000){
				state.bpmAlarmActive = true;
				pushAlert('bpm-low', `BPM low: ${currentBpm.toFixed(1)} (< 60) for 10s`);
				alarmStartContinuous();
			}else if(state.bpmAlarmActive){
				// keep alarm sounding while still low
				alarmStartContinuous();
			}
		}else{
			state.bpmBelowStartMs = 0;
			if(state.bpmAlarmActive){ alarmStopContinuous(); }
			state.bpmAlarmActive = false;
		}
	}

	function connectWS(){
		if(!state.token) return;
		const url = new URL(window.location.origin.replace('http','ws'));
		url.pathname = '/ws';
		url.searchParams.set('token', state.token);
		const ws = new WebSocket(url);
		state.ws = ws;
		ws.onopen = ()=>setConnected(true);
		ws.onclose = ()=>setConnected(false);
		ws.onerror = ()=>setConnected(false);
		ws.onmessage = (ev)=>{
			try {
				const msg = JSON.parse(ev.data);
				// Handle server-computed BPM messages with smoothing
				if(msg && msg.type === 'bpm' && typeof msg.bpm === 'number'){
					state.bpm = msg.bpm;
					state.bpmRawSeries.push(msg.bpm);
					// Rolling average over last N values
					const N = state.bpmSmoothingWindow;
					const len = state.bpmRawSeries.length;
					const start = Math.max(0, len - N);
					let sum = 0, c = 0;
					for(let i=start;i<len;i++){ const v = state.bpmRawSeries[i]; if(typeof v === 'number'){ sum += v; c++; } }
					const smooth = c ? (sum / c) : msg.bpm;
					if(state.bpmSeries.length > 0){
						state.bpmSeries[state.bpmSeries.length - 1] = smooth;
					}else{
						state.bpmSeries.push(smooth);
					}
					state.lastBpm = smooth;
					els.bpmLabel.textContent = smooth.toFixed(1);
					checkBpmAlarm(smooth);
					scheduleChartUpdate();
					return;
				}
				if(msg && (typeof msg.sensor1 !== 'undefined' || typeof msg.sensor2 !== 'undefined')){
					onSample(msg);
				}
			} catch(e) { /* ignore */ }
		};
		setInterval(()=>{ try { ws.readyState===1 && ws.send('ping'); } catch(e){} }, 5000);
	}

	function showSession(){
		els.authForms.classList.add('hidden');
		els.authSession.classList.remove('hidden');
		els.userLabel.textContent = state.username;
		els.deviceKey.textContent = state.deviceKey || '—';
	}
	function showAuth(){
		els.authForms.classList.remove('hidden');
		els.authSession.classList.add('hidden');
		els.deviceKey.textContent = '—';
		setConnected(false);
	}

	async function doLogin(isRegister){
		const username = els.username.value.trim();
		const password = els.password.value;
		if(!username || !password) return;
		try{
			if(isRegister){
				const r = await fetch('/auth/register', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username, password }) });
				if(!r.ok){ const txt = await r.text(); throw new Error(`Register failed (${r.status}): ${txt}`); }
				const data = await r.json();
				state.token = data.access_token; state.username = username; state.deviceKey = data.device_key;
			}else{
				const form = new URLSearchParams();
				form.set('username', username); form.set('password', password); form.set('grant_type', 'password');
				const r = await fetch('/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' }, body: form.toString() });
				if(!r.ok){ const txt = await r.text(); throw new Error(`Login failed (${r.status}): ${txt}`); }
				const data = await r.json();
				state.token = data.access_token; state.username = username; state.deviceKey = data.device_key;
			}
			await openDB(state.username);
			showSession();
			connectWS();
		} catch(e){
			console.error(e);
			alert(String(e.message || e));
		}
	}

	function logout(){
		state.token = null; state.username = null; state.deviceKey = null;
		if(state.ws) try{ state.ws.close(); }catch(e){}
		showAuth();
	}

	async function sendTestSample(){
		if(!state.deviceKey){ alert('No Device Key. Login first.'); return; }
		// Send with legacy keys; backend accepts and normalizes
		const payload = { timestamp: Date.now(), sensor1: Math.random()*50+50, sensor2: Math.random()*30+20, sensor3: 0 };
		try{
			const r = await fetch('/ingest', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Device-Key': state.deviceKey }, body: JSON.stringify(payload) });
			if(!r.ok){ const txt = await r.text(); throw new Error(`Ingest failed (${r.status}): ${txt}`); }
		}catch(e){ alert(String(e.message||e)); }
	}

	function bindUI(){
		els.btnLogin.addEventListener('click', ()=>doLogin(false));
		els.btnRegister.addEventListener('click', ()=>doLogin(true));
		els.btnLogout.addEventListener('click', logout);
		els.btnExport.addEventListener('click', exportCSV);
		els.btnTest.addEventListener('click', sendTestSample);
		els.toggleLog.addEventListener('change', (e)=>{ state.logEnabled = e.target.checked; });
		els.windowSeconds.addEventListener('change', ()=>{ state.windowSeconds = Math.max(1, Math.min(60, parseInt(els.windowSeconds.value||'10',10))); });
		els.apneaWindow.addEventListener('change', ()=>{ state.apneaWindowSec = Math.max(3, Math.min(60, parseInt(els.apneaWindow.value||'10',10))); });
		els.apneaThreshold.addEventListener('change', ()=>{ state.apneaMvThreshold = Math.max(1, Math.min(1000, parseInt(els.apneaThreshold.value||'10',10))); });
		if(els.srSlider && els.srThresholdLabel && els.srApply){
			els.srThresholdLabel.textContent = String(els.srSlider.value);
			els.srSlider.addEventListener('input', ()=>{ els.srThresholdLabel.textContent = String(els.srSlider.value); });
			els.srApply.addEventListener('click', ()=>{ state.srThreshold = Math.max(10, Math.min(1000, parseInt(els.srSlider.value||'900',10))); pushAlert('info', `Low SR threshold set to ${state.srThreshold} Hz`); });
		}
		els.alertSound.addEventListener('change', ()=>{ state.alertSound = els.alertSound.checked; });
	}

	function init(){
		bindUI();
		initCharts();
		// Initialize SR threshold from slider if present
		if(els.srSlider){
			const v = parseInt(els.srSlider.value||'900', 10);
			if(!isNaN(v)) state.srThreshold = Math.max(10, Math.min(1000, v));
			if(els.srThresholdLabel) els.srThresholdLabel.textContent = String(v);
		}
	}

	init();
})();


