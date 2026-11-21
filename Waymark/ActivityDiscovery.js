// Shared by the personal Mac collector and iPhone. Output is a search plan, never a recommendation.
function planActivities(sources, attempted) {
    const groups = {routes: new Map(), experiences: new Map(), places: new Map()};
    const combinations = new Map();
    const route = /[大中小内外东西南北]{1,3}(?:圈|环线)|[东西南北]线|[\u3400-\u9fff]{2,8}(?:步道|骑行线|徒步线|风景道|环岛线)/g;
    const action = /[\u3400-\u9fff]{2,5}(?:制作课|体验课|工坊|音乐节)|水上(?:村落|浮村|市场)|(?:湖上|海上|山顶)?(?:日落|落日|日出)|夜游|夜市|露营|赶海|观鲸|浮潜|潜水|滑雪|滑冰|观鸟|徒步|骑行|游船|坐船|划船|皮划艇|漂流|跳岛|温泉|早市|市集|巡礼|看展|壁画|手作|茶道|戏曲|演出|赏花|赏枫|缆车|观星|采摘|City\s?walk/gi;
    const place = /[\u3400-\u9fff]{1,10}(?:广场|地质公园|博物馆|美术馆|国家公园|老街|古镇|湿地|公园|市场|古城|海滩|海岸|瀑布|步道|灯塔|峡谷|湖|岛|湾|寺|山路|山|滩)/g;
    function record(group, term, source, line) {
        if (!term || term.length < 2 || term.length > 16) return;
        // Only retain a literal lead and the specific note in which it appeared.
        if (!line.includes(term)) return;
        const map = groups[group];
        const key = term.toLowerCase().replace(/\s/g, '');
        if (!map.has(key)) map.set(key, {term, evidence: []});
        const entry = map.get(key);
        if (!entry.evidence.some(e => e.id === source.id)) {
            const start = Math.max(0, line.indexOf(term) - 28);
            entry.evidence.push({id: source.id, author: source.author || '', term, quote: line.slice(start, start + 100)});
        }
    }
    for (const source of sources) {
        if (!source.text || !source.id) continue;
        for (const paragraph of source.text.split(/\n/)) {
            // Learn author-defined itineraries, including names outside our travel vocabulary.
            const heading = paragraph.match(/^\s*(?:day\s*\d+|第[一二三四五六七八九十0-9]+天|(?:线路|路线|玩法)[一二三四五六七八九十0-9]+)\s*[：:·・|｜、.\-—]?\s*(.{2,24}?)\s*$/i);
            if (heading && !/[\d:：]|出发前|返程|落地|签到|住宿|早餐|入住/.test(heading[1]))
                record('routes', heading[1].trim(), source, paragraph);

            // Also learn the author's own names for local activities from labeled lists.
            const named = paragraph.match(/^\s*(?:特色体验|推荐玩法|当地活动|体验项目|打卡地点|推荐景点)\s*[：:]\s*([^。！？；]{2,60})(?:[。！？；]|$)/);
            if (named) {
                const group = /打卡地点|推荐景点/.test(named[0].slice(0,6)) ? 'places' : 'experiences';
                for (const part of named[1].split(/[、，,｜|]/)) {
                    const term = part.trim();
                    if (term.length <= 12) record(group, term, source, paragraph);
                }
            }

            const paragraphPlaces = new Set();
            const paragraphActions = new Set();
            for (const line of paragraph.split(/[。！？；]/)) {
                for (const term of line.match(route) || []) record('routes', term, source, line);
                for (const term of line.match(action) || []) {
                    record('experiences', term, source, line);
                    paragraphActions.add(term);
                }
                for (const match of line.match(place) || []) {
                    const term = match.split(/(?:前往|抵达|到达|游览|打车到|出发去|再去|位于|可以去|推荐去|顺路去|顺路|对面|扎进|路线看|顺道看|游览完|参观|打卡|逛完|路过|靠近|近|去|到|从|在|和|是|的)/).pop();
                    if (term && term.length >= 2 && !/^(?:附近|旁边|这里|一个|周边|这家|当地|国家|地质|市区|很多|水上|美术|博物|景区)(?:公园|市场|湖|寺|山|岛)?$/.test(term)) {
                        record('places', term, source, line);
                        paragraphPlaces.add(term);
                    }
                }
            }
            // Only combine a place and activities in the same short passage about one named site.
            if (paragraph.length <= 320 && paragraphPlaces.size === 1 && paragraphActions.size) {
                const placeName = Array.from(paragraphPlaces)[0];
                const terms = Array.from(paragraphActions).sort((a,b)=>b.length-a.length).slice(0,3);
                const key = placeName + ' ' + terms.join(' ');
                if (!combinations.has(key)) combinations.set(key, {placeName, terms, authors: new Set()});
                if (source.author) combinations.get(key).authors.add(source.author);
            }
        }
    }
    const leads = {};
    for (const [kind, map] of Object.entries(groups)) {
        leads[kind] = Array.from(map.values(), entry => ({
            term: entry.term, authors: new Set(entry.evidence.map(e => e.author).filter(Boolean)).size,
            evidence: entry.evidence
        })).sort((a,b) => b.evidence.length - a.evidence.length);
    }
    const defaults = {routes:'旅行区域 经典线路 如何选择', experiences:'当地特色体验 半日游 实际体验', places:'周边值得去的地方 一日游'};
    const used = new Set(attempted.map(q => q.query));
    const next = [];
    for (const kind of ['routes','experiences','places']) {
        if (attempted.some(q => q.kind === kind)) continue;
        const options = leads[kind].filter(x => x.authors < 2);
        const combo = kind === 'experiences' && Array.from(combinations.values())
            .filter(c=>c.authors.size < 2).sort((a,b)=>b.terms.length-a.terms.length)[0];
        if (!combo && !options.length && leads[kind].length) continue;
        let terms = options.slice(0,kind === 'routes' ? 3 : 1).map(x=>x.term);
        if (combo) terms = [combo.placeName, ...combo.terms];
        const query = terms.length ? terms.join(' ') + (kind === 'routes' ? ' 线路 区别' : ' 游玩 实测') : defaults[kind];
        if (!used.has(query)) next.push({kind, query});
    }
    return {leads, next};
}
if (typeof module !== 'undefined' && require.main === module) {
    const {sources, attempted} = JSON.parse(require('fs').readFileSync(0,'utf8'));
    process.stdout.write(JSON.stringify(planActivities(sources, attempted)));
}
