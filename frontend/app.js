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
		updatePending: false,
		alerts: [],
		apneaWindowSec: 10,
		apneaMvThreshold: 10,
		srThreshold: 900,
		alertSound: true,
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
		srThreshold: document.getElementById('sr-threshold'),
		alertSound: document.getElementById('alert-sound'),
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
			state.chart.update('none');
			state.chart2.update('none');
			state.chart3.update('none');
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
				y: { title: { display: true, text: 'mV' }, grid: { color: 'rgba(255,255,255,0.06)' } },
			},
			plugins: { legend: { display: false } },
		};
		state.chart = new Chart(els.c1.getContext('2d'), {
			type: 'line',
			data: { labels: state.buffers.labels, datasets: [{ data: state.buffers.s1, borderColor: '#2f81f7', borderWidth: 1, pointRadius: 0 }] },
			options: common,
		});
		state.chart2 = new Chart(els.c2.getContext('2d'), {
			type: 'line',
			data: { labels: state.buffers.labels, datasets: [{ data: state.buffers.s2, borderColor: '#2ea043', borderWidth: 1, pointRadius: 0 }] },
			options: common,
		});
		state.chart3 = new Chart(els.c3.getContext('2d'), {
			type: 'line',
			data: { labels: state.buffers.labels, datasets: [{ data: state.buffers.s3, borderColor: '#f85149', borderWidth: 1, pointRadius: 0 }] },
			options: common,
		});
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
		}
		scheduleChartUpdate();
		checkApnea();
	}

	function pushAlert(type, text){
		const ts = new Date();
		state.alerts.push({ type, text, ts });
		const li = document.createElement('li');
		li.className = `alert-item ${type}`;
		li.innerHTML = `<span>${text}</span><span class="time">${ts.toLocaleTimeString()}</span>`;
		els.alertList.prepend(li);
		if(els.alertList.children.length > 100){ els.alertList.removeChild(els.alertList.lastChild); }
		if(state.alertSound){ beep(); }
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
			pushAlert('low-sr', `Low sample rate: ${state.lastSecondCount} Hz (< ${state.srThreshold} Hz)`);
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
		els.srThreshold.addEventListener('change', ()=>{ state.srThreshold = Math.max(10, Math.min(1000, parseInt(els.srThreshold.value||'900',10))); });
		els.alertSound.addEventListener('change', ()=>{ state.alertSound = els.alertSound.checked; });
	}

	function init(){
		bindUI();
		initCharts();
	}

	init();
})();


