// 管理员代发 / 代唱（owner-only relay）——两条 HTTP 路由的**完整实现**。
//
// 为什么单独抽成一个文件而不是让你往 bridge.js 里粘 400 行：
//   bridge.js 每个版本都不一样，行号一漂就粘错位置。这里把逻辑封装成一个工厂，
//   你在 bridge.js 里只要加「一个 deps 对象 + 两行分发」，位置无关、不会串行。
//
// 这两条路由解决的是同一个问题：**会话令牌是按会话签发的**（agentTokenOk 只认自己那个
// key），所以 qq_send_private_message / qq_sing 天生发不到别的会话去，就算目标在好友
// 白名单里也必然 403。这里在不放弃会话隔离的前提下开一个极窄的口子。
//
// 五条闸门必须同时成立（缺一不可，缺了就是提权）：
//   ① 发起方 key 正好等于 private:<ownerQQ>（管理员私聊）；
//   ② 发起方令牌属于这个会话（证明确实是那个会话里的 AI 在调）；
//   ③ 目标命中当前模式的 allow/deny（与正常发送完全同一口径）；
//   ④ 照旧过 maxMessageChars、敏感信息审计、每分钟/每小时限流；
//   ⑤ 发出去的内容写进**目标会话**的历史（recordSentMessagesV2），否则那边的 AI
//      醒来会不知道自己「说过这句」，前后矛盾。
// 群友在任何会话里都满足不了 ①，所以群聊里的 prompt injection 拿不到这个口子。
//
// 代唱额外一层：非白名单目标只允许 song（songOnly），不允许夹带文字/图片/表情/引用。
// 也就是说「唱歌可以发给任何真实好友」，但「替机器人发消息」仍然只限白名单。

import { parseRelayBilibiliSong } from './bilibili-song.js';

const RELAY_TARGETS_PATH = '/api/socialV2/relay-targets';
const RELAY_SEND_PATH = '/api/socialV2/relay-send';

/**
 * @param {object} deps 全部由 bridge.js 注入（它内部有这些闭包，外部拿不到）
 * @returns {{ paths: string[], handle: (req, res, url) => Promise<boolean> }}
 */
export function createRelayRoutes(deps) {
  const {
    getConfig, path, fs, log = () => {}, appendActivity = () => {},
    sendJson, readBody,
    agentTokenOk, v2ToolEnabled, modeAllowed, canonicalV2Key,
    isSessionAllowedInCurrentMode, shouldBlockSilentReply,
    getCurrentMode, isPaused,
    coverJobs, relaySongScope, submitTrackedSong,
    imageSendRoots, isInsideAllowedImageDir, imageSendMaxPerHour,
    sendImageBufferToKey, sendMessagesV2, sendStickerV2, computeGapsV2,
    findMessageMedia, resolveReplyTargetV2,
    getSocialV2State, saveSocialV2State, recordSentMessagesV2, scheduleReplyCheckV2,
    MAX_MEDIA_BYTES, SENSITIVE_RE, KNOWN_AGENT_TOKENS,
    safeFetchBuffer
  } = deps;

  const cfgOf = () => getConfig();
  const ownerKeyOf = () => {
    const ownerQQ = cfgOf()?.ownerQQ;
    return ownerQQ ? `private:${String(ownerQQ)}` : '';
  };

  // 缺一个 deps 就会在**某次真实调用**里抛 undefined，而且报错位置离原因很远
  // （第一次踩会以为是路由写错了）。所以在装配期一次性点名报出来。
  //
  // 分两档：
  //   · 必需 —— 没有它连「代发一句文字」都做不到，缺了直接拒绝启动；
  //   · 可选 —— 只影响某一种内容形态（比如代发本地图片），缺了就把那条路关掉，
  //     其余功能照常。上游 qq-bridge 没有生图目录白名单那套东西，属于这一档。
  for (const [name, value] of Object.entries({
    getConfig, sendJson, readBody, agentTokenOk, v2ToolEnabled, modeAllowed,
    canonicalV2Key, isSessionAllowedInCurrentMode, getCurrentMode, isPaused,
    coverJobs, relaySongScope, submitTrackedSong,
    sendMessagesV2, sendStickerV2, computeGapsV2,
    findMessageMedia, resolveReplyTargetV2, getSocialV2State, saveSocialV2State,
    recordSentMessagesV2, scheduleReplyCheckV2, safeFetchBuffer
  })) {
    if (value === undefined || value === null) {
      throw new TypeError(
        `createRelayRoutes: 缺少必需的 deps.${name}。` +
        ' 这些闭包都在 bridge.js 的 startConsoleServer() 里，必须显式传进来；' +
        ' 完整清单见 docs/BRIDGE_WIRING.md「deps 清单」。'
      );
    }
  }

  // 代发本地图片（imagePath）需要 bridge.js 的生图目录白名单。没有就关掉这条路，
  // 并在被调用时给出可操作的提示 —— 而不是让它在读文件那一步莫名其妙地失败。
  const imageRelayAvailable = !!(sendImageBufferToKey && imageSendRoots
    && isInsideAllowedImageDir && imageSendMaxPerHour);

  // ── 目标清单（只读）────────────────────────────────────────────────────
  // 只给 owner 私聊会话看：白名单里有谁、群叫什么。群友会话拿不到（key 必须正好是
  // private:<ownerQQ>，且令牌必须属于那个会话），所以不会把管理信息泄进群聊上下文。
  async function handleTargets(req, res, url) {
    const cfg = cfgOf();
    const key = String(url.searchParams.get('key') ?? '').trim();
    const token = String(req.headers['x-agent-token'] ?? '').trim();
    const ownerKey = ownerKeyOf();
    if (!ownerKey) { sendJson({ ok: false, error: '未配置 ownerQQ（config.json），代发不可用' }, 403); return; }
    if (key !== ownerKey) { sendJson({ ok: false, error: '只有管理员私聊会话可以查看代发目标' }, 403); return; }
    if (!token || !agentTokenOk(key, token)) { sendJson({ ok: false, error: 'agent token 无效' }, 403); return; }
    if (!v2ToolEnabled('relayTargets')) { sendJson({ ok: false, error: '工具未启用：qq_relay_targets' }, 403); return; }
    const contacts = await relaySongScope.list();
    const mode = getCurrentMode();
    const permissions = (t) => {
      const [kind, id] = t.target.split(':');
      return { ...t, canRelaySong: true, canRelayMessages: modeAllowed(t.target, kind, Number(id), cfg, mode) };
    };
    sendJson({
      ok: true,
      private: contacts.private.map(permissions),
      groups: contacts.groups.map(permissions),
      hint: '这是当前QQ真实好友和已加入群（显式deny仍拒绝）。管理员可对canRelaySong=true目标代唱，target原样使用；多人逐个提交，重名先问。canRelayMessages=false时只能提交song，不能夹带文字、表情、图片或引用；不会开放目标自动聊天。'
    });
  }

  // ── 发送 ───────────────────────────────────────────────────────────────
  async function handleSend(req, res) {
    const cfg = cfgOf();
    const body = await readBody();
    const key = String(body.key ?? '').trim();
    const token = String(body.token ?? '').trim();
    const targetRaw = String(body.target ?? '').trim();
    const ownerKey = ownerKeyOf();
    const mode = getCurrentMode();

    if (!ownerKey) { sendJson({ ok: false, error: '未配置 ownerQQ（config.json），代发不可用' }, 403); return; }
    if (key !== ownerKey) { sendJson({ ok: false, error: '只有管理员私聊会话可以代发消息' }, 403); return; }
    if (!token || !agentTokenOk(key, token)) { sendJson({ ok: false, error: 'agent token 无效' }, 403); return; }
    if (!v2ToolEnabled('relaySend')) { sendJson({ ok: false, error: '工具未启用：qq_relay_send' }, 403); return; }
    if (!isSessionAllowedInCurrentMode(key)) { sendJson({ ok: false, error: '管理员会话不在当前模式允许范围内' }, 403); return; }

    const target = canonicalV2Key(targetRaw);
    if (!target) { sendJson({ ok: false, error: 'target 格式应为 private:QQ号 或 group:群号' }, 400); return; }
    if (target === key) { sendJson({ ok: false, error: '不能代发给自己：要给当前会话发言请直接用 qq_send_message' }, 400); return; }

    const targetMatch = /^(group|private):(\d+)$/.exec(target);
    const tKind = targetMatch[1];
    const tId = Number(targetMatch[2]);
    const targetNormallyAllowed = modeAllowed(target, tKind, tId, cfg, mode);

    // 非白名单目标：只允许「纯代唱」。只要还带了别的任何内容就拒绝。
    const songOnly = String(body.song ?? '').trim() &&
      ['messages', 'stickerId', 'forwardFromMessageId', 'imagePath', 'replyToMessageId']
        .every((k) => body[k] === undefined || body[k] === null || body[k] === '');
    if (!targetNormallyAllowed && !songOnly) {
      sendJson({ ok: false, error: '非聊天白名单目标仅允许管理员代唱song，不可混发文字、图片、表情或引用' }, 403);
      return;
    }
    if (!targetNormallyAllowed) await relaySongScope.assertMember(target);

    // 消息体归一化与 /api/socialV2/send-message 完全一致：字符串=一条，数组=多条，
    // 并兼容模型把数组/单条序列化成 JSON 字符串传进来的情况。
    let rawMessages = body.messages;
    if (typeof rawMessages === 'string') {
      const trimmed = rawMessages.trim();
      if (trimmed.startsWith('[')) {
        try {
          const parsed = JSON.parse(trimmed);
          if (Array.isArray(parsed)) rawMessages = parsed.map(String);
        } catch {}
      } else if (trimmed.startsWith('"')) {
        try {
          const parsed = JSON.parse(trimmed);
          if (typeof parsed === 'string') rawMessages = parsed;
          else if (Array.isArray(parsed)) rawMessages = parsed.map(String);
        } catch {}
      }
    }
    const messages = Array.isArray(rawMessages)
      ? rawMessages.map((m) => String(m ?? '').trim()).filter(Boolean)
      : (typeof rawMessages === 'string' ? [rawMessages.trim()].filter(Boolean) : []);

    const stickerId = String(body.stickerId ?? '').trim();
    const forwardFromMessageId = String(body.forwardFromMessageId ?? '').trim();
    const relaySong = String(body.song ?? '').trim();
    const relaySongMode = String(body.mode ?? 'full').trim().toLowerCase();
    let relayVideo = null;
    const relayImagePathRaw = String(body.imagePath ?? '').trim();

    if (!messages.length && !stickerId && !forwardFromMessageId && !relaySong && !relayImagePathRaw) {
      sendJson({ ok: false, error: '至少要有一种内容：messages（文字）/ stickerId（收藏表情）/ forwardFromMessageId（转发本会话某条消息里的图片）/ imagePath（生图目录里的本地图片）/ song（代目标会话点一首歌）' }, 400);
      return;
    }

    // 代发本地图片：目录边界必须与 qq_send_image 完全一致，否则代发就是绕开目录边界的旁路。
    let relayImageBuffer = null;
    let relayImageName = '';
    if (relayImagePathRaw) {
      if (!imageRelayAvailable) {
        sendJson({ ok: false, error: '这个桥接没接生图目录白名单（sendImageBufferToKey / imageSendRoots），代发本地图片不可用；文字、表情、转发图片、代唱都照常。接法见 docs/BRIDGE_WIRING.md' }, 501);
        return;
      }
      if (!v2ToolEnabled('sendImage')) { sendJson({ ok: false, error: '工具未启用：qq_send_image' }, 403); return; }
      if (mode !== 'reserved2') { sendJson({ ok: false, error: '代发图片仅在 reserved2 模式可用' }, 403); return; }
      const relayAbsPath = path.resolve(relayImagePathRaw);
      const roots = imageSendRoots();
      if (!isInsideAllowedImageDir(relayAbsPath, roots)) {
        sendJson({ ok: false, error: `只允许代发绘图目录内的文件（当前允许：${roots.join(' ; ')}）` }, 403);
        return;
      }
      if ([...KNOWN_AGENT_TOKENS].some((t) => t && relayAbsPath.includes(t))) {
        sendJson({ ok: false, error: '路径含会话令牌，已阻止发送' }, 403);
        return;
      }
      try {
        relayImageBuffer = await fs.promises.readFile(relayAbsPath);
      } catch (error) {
        sendJson({ ok: false, error: `读不到图片：${error?.message ?? error}` }, 400);
        return;
      }
      if (!relayImageBuffer.length) { sendJson({ ok: false, error: '图片是空文件' }, 400); return; }
      if (relayImageBuffer.length > MAX_MEDIA_BYTES) {
        sendJson({ ok: false, error: `图片 ${(relayImageBuffer.length / 1048576).toFixed(1)}MB 超过单张上限 ${Math.round(MAX_MEDIA_BYTES / 1048576)}MB，请降低 megapixels 或画幅后重画` }, 413);
        return;
      }
      relayImageName = path.basename(relayAbsPath);
    }

    if (relaySong) {
      if (mode !== 'reserved2') { sendJson({ ok: false, error: '代点歌仅在 reserved2 模式可用' }, 403); return; }
      if (isPaused()) { sendJson({ ok: false, error: '二代 AI 已暂停，不能代点歌' }, 403); return; }
      if (!v2ToolEnabled('sing')) { sendJson({ ok: false, error: '工具未启用：qq_sing' }, 403); return; }
      if (cfg.socialV2?.sing?.enabled === false) { sendJson({ ok: false, error: '点歌功能已关闭（socialV2.sing.enabled=false）' }, 403); return; }
      if (relaySongMode !== 'chorus' && relaySongMode !== 'full') { sendJson({ ok: false, error: 'mode 只能是 chorus（副歌试听）或 full（整首）' }, 400); return; }
      if (SENSITIVE_RE.test(relaySong) || [...KNOWN_AGENT_TOKENS].some((t) => t && relaySong.includes(t))) {
        sendJson({ ok: false, error: '点歌内容含敏感信息，已阻止' }, 403);
        return;
      }
      try { relayVideo = parseRelayBilibiliSong(relaySong); }
      catch (error) { sendJson({ ok: false, error: error.message }, 400); return; }
      if (!relayVideo && relaySong.length > 120) { sendJson({ ok: false, error: '歌名太长，请给一个具体歌名（可以带歌手）' }, 400); return; }
      if (!relayVideo && /[\n；;]/.test(relaySong)) { sendJson({ ok: false, error: '一次只能代点一首歌，请给出单个歌名（可带歌手）' }, 400); return; }
    }

    const sendCfgRelay = cfg.socialV2?.send ?? {};
    const burstCap = Math.max(1, Number(sendCfgRelay.burstMaxMessages) || 8);
    if (messages.length > burstCap) { sendJson({ ok: false, error: `一次最多代发 ${burstCap} 条` }, 400); return; }
    const maxChars = Math.max(1, Number(sendCfgRelay.maxMessageChars) || 500);
    for (const m of messages) {
      if (m.length > maxChars) { sendJson({ ok: false, error: `单条消息不能超过 ${maxChars} 字` }, 400); return; }
      if (SENSITIVE_RE.test(m)) { sendJson({ ok: false, error: '消息含敏感信息，已阻止发送' }, 403); return; }
    }

    // 转发图片的源必须是**发起方会话里真收过的消息**（findMessageMedia 只查本地已存的
    // 媒体元数据）。不接受外部 URL —— 否则这个工具就成了「用机器人账号替任何人发任意图片」。
    let forwardImages = [];
    if (forwardFromMessageId) {
      const media = findMessageMedia(key, forwardFromMessageId);
      const candidates = (Array.isArray(media) ? media : [])
        .filter((m) => m && (m.kind === 'image' || m.kind === 'sticker') && m.url);
      if (!candidates.length) {
        sendJson({ ok: false, error: `消息 ${forwardFromMessageId} 里没有可转发的图片/表情（只支持发送方会话里确实带图的消息）` }, 400);
        return;
      }
      const maxImages = 3;
      if (candidates.length > maxImages) {
        sendJson({ ok: false, error: `这条消息里有 ${candidates.length} 个图片/表情，一次最多转发 ${maxImages} 个` }, 400);
        return;
      }
      forwardImages = candidates;
    }

    let actualReplyTo = body.replyToMessageId;
    let quotedInfo = null;
    if (actualReplyTo !== undefined && actualReplyTo !== null && String(actualReplyTo).trim() !== '') {
      const rid = String(actualReplyTo).trim();
      if (!/^-?[1-9]\d*$/.test(rid)) { sendJson({ ok: false, error: 'replyToMessageId 必须是非零整数（消息 id 可能为负数）' }, 400); return; }
      // 引用必须指向**目标会话**里的消息：拿管理员会话的 id 去引用别处的消息没有意义。
      const resolved = await resolveReplyTargetV2(getSocialV2State(target), tKind, tId, rid);
      if (!resolved) {
        sendJson({ ok: false, error: '无法解析被引用消息：该 id 必须属于目标会话（可先用 qq_get_recent_messages 查目标会话）' }, 400);
        return;
      }
      quotedInfo = resolved.info;
      actualReplyTo = resolved.messageId;
    }

    const delays = computeGapsV2(messages, 'auto', 0, [], sendCfgRelay);
    const stTarget = getSocialV2State(target);
    const now = Date.now();
    // 一次代发要占用目标会话多少条额度：文字条数 + 表情 1 + 每张图 1。
    const sendUnits = messages.length + (stickerId ? 1 : 0) + forwardImages.length + (relayImageBuffer ? 1 : 0);
    const historyEntries = [];

    try {
      // 限流记在**目标会话**头上：代发不能变成绕过该会话发送额度的后门。
      const maxPerMinute = Number(sendCfgRelay.maxSendPerMinute) || 0;
      const maxPerHour = Number(sendCfgRelay.maxSendPerHour) || 0;
      const recentMinute = (stTarget.sendTimes || []).filter((t) => now - t < 60000).length;
      const recentHour = (stTarget.sendTimes || []).filter((t) => now - t < 3600000).length;
      if ((maxPerMinute > 0 && recentMinute + sendUnits > maxPerMinute)
          || (maxPerHour > 0 && recentHour + sendUnits > maxPerHour)) {
        sendJson({ ok: false, error: `目标会话发送频率超限（${target}），请稍后再试` }, 429);
        return;
      }
      for (let i = 0; i < sendUnits; i++) stTarget.sendTimes.push(now);
      if (stTarget.sendTimes.length > 500) stTarget.sendTimes = stTarget.sendTimes.slice(-500);

      // 代发图片同样吃目标会话的「每小时图片张数」额度：否则代发会变成绕过该限额的后门。
      if (relayImageBuffer) {
        const maxImagePerHour = imageSendMaxPerHour();
        if (!Array.isArray(stTarget.imageSendTimes)) stTarget.imageSendTimes = [];
        stTarget.imageSendTimes = stTarget.imageSendTimes.filter((t) => Number.isFinite(t) && now - t < 3600000);
        if (maxImagePerHour > 0 && stTarget.imageSendTimes.length + 1 > maxImagePerHour) {
          for (let i = 0; i < sendUnits; i++) {
            const idx = stTarget.sendTimes.indexOf(now);
            if (idx >= 0) stTarget.sendTimes.splice(idx, 1);
          }
          saveSocialV2State();
          sendJson({ ok: false, error: `目标会话一小时内最多发 ${maxImagePerHour} 张图，请稍后再试` }, 429);
          return;
        }
      }

      let textSent = 0;
      if (messages.length) {
        const sent = await sendMessagesV2(target, messages, delays, actualReplyTo, null);
        textSent = sent.length;
        historyEntries.push(...sent);
      }
      let stickerOk = false;
      if (stickerId) {
        // 表情走 sendStickerV2：它自带白名单守卫（captureSendGuard）与限速，
        // 并且会先同步一次收藏表情库，刚存的表情也能立刻用。
        await sendStickerV2(target, stickerId, { replyToMessageId: messages.length ? null : actualReplyTo });
        stickerOk = true;
        historyEntries.push('[表情]');
      }
      let imageSent = 0;
      for (let i = 0; i < forwardImages.length; i++) {
        const item = forwardImages[i];
        const fetched = await safeFetchBuffer(String(item.url), MAX_MEDIA_BYTES);
        await sendImageBufferToKey(target, fetched.buffer, {
          replyToMessageId: (i === 0 && !messages.length && !stickerId) ? actualReplyTo : null
        });
        imageSent += 1;
        historyEntries.push('[图片]');
      }
      if (relayImageBuffer) {
        await sendImageBufferToKey(target, relayImageBuffer, {
          replyToMessageId: (!messages.length && !stickerId && !forwardImages.length) ? actualReplyTo : null
        });
        imageSent += 1;
        historyEntries.push('[图片]');
        stTarget.imageSendTimes.push(Date.now());
        if (stTarget.imageSendTimes.length > 500) stTarget.imageSendTimes = stTarget.imageSendTimes.slice(-500);
        log(`[image] 代发图片 ${target}: ${relayImageName} (${Math.round(relayImageBuffer.length / 1024)}KB)`);
      }

      // 代点歌放在文字/表情/图片之后：管理员常常是「先跟对方说一声，再点歌」。
      // 提交失败要让整次代发明确失败（不吞错误），但已经发出去的文字不回滚。
      let songInfo = null;
      if (relaySong) {
        const selected = !relayVideo && typeof coverJobs.resolveSong === 'function'
          ? await coverJobs.resolveSong({ autoOriginalQuery: relaySong })
          : null;
        const relayGrant = await relaySongScope.issue({ ownerKey: key, target });
        const res = await submitTrackedSong({
          relayGrant,
          song: relayVideo ? 'B站视频 ' + relayVideo.url : (selected?.song || relaySong),
          mode: relaySongMode,
          target,
          // 失败/复核这类终止通知发给管理员（当前会话），不发目标会话：
          // 目标只该收到做好了的语音，不该收到「版本需确认」这种它没法处理的提示。
          notifyKey: key,
          ...(relayVideo ? { videoUrl: relayVideo.url } : (selected?.trackId ? { trackId: selected.trackId } : {}))
        });
        if (!res?.job || res?.ok === false) throw Object.assign(new Error('代点歌未取得有效任务号，不能确认提交成功'), { statusCode: 502 });
        relaySongScope.bind(relayGrant, target, res.job);
        songInfo = {
          sourceKind: relayVideo ? 'bilibili' : 'netease',
          job: res?.job ?? null,
          song: res?.song ?? relaySong,
          mode: res?.mode ?? relaySongMode,
          status: res?.status ?? null,
          queuePosition: res?.queuePosition ?? null,
          etaHuman: res?.etaHuman ?? null,
          note: res?.note ?? null
        };
      }

      if (historyEntries.length) recordSentMessagesV2(target, historyEntries);
      stTarget.lastAiReplyAt = Date.now();
      stTarget.lastActionAt = Date.now();
      stTarget.wakeConfig.noActionCount = 0;
      // 发起方（管理员会话）也算「这一轮确实做了一件事」：清掉无动作计数，
      // 否则 AI 明明代发了，却因为自己那个会话没发言而被唠叨/降频。
      const stOwner = getSocialV2State(key);
      stOwner.lastActionAt = Date.now();
      stOwner.wakeConfig.noActionCount = 0;
      saveSocialV2State();

      const sentTotal = textSent + (stickerOk ? 1 : 0) + imageSent;
      const totalImages = forwardImages.length + (relayImageBuffer ? 1 : 0);
      const detail = [
        messages.length ? `文字 ${textSent}/${messages.length}` : '',
        stickerId ? `表情 ${stickerOk ? '1/1' : '0/1'}` : '',
        totalImages ? `图片 ${imageSent}/${totalImages}` : '',
        relaySong ? `点歌 ${songInfo?.job ? 'job=' + songInfo.job : '未拿到任务号'}` : ''
      ].filter(Boolean).join('，');
      log(`[relay] ${key} → ${target}: 成功 ${sentTotal}/${sendUnits}（${detail}）`);
      appendActivity(`${key} [relay] → ${target} 成功 ${sentTotal}/${sendUnits}（${detail}）`);
      if (sentTotal > 0) scheduleReplyCheckV2(target);
      sendJson({
        ok: true,
        key,
        target,
        sent: sentTotal,
        failed: sendUnits - sentTotal,
        breakdown: { text: textSent, sticker: stickerOk ? 1 : 0, images: imageSent, localImage: relayImageName || null },
        sing: songInfo,
        quoted: quotedInfo
      });
    } catch (error) {
      if (error?.sent?.length) {
        recordSentMessagesV2(target, error.sent);
        log(`[relay] 部分成功 ${error.sent.length}/${messages.length} 条文字，已记录已发消息`);
      }
      // 失败时回滚预占的额度，避免假 429（按整批回滚：代发是原子操作，
      // 中途失败时把剩余额度还回去比逐条精确核算更重要）。
      for (let i = 0; i < sendUnits; i++) {
        const idx = stTarget.sendTimes.indexOf(now);
        if (idx >= 0) stTarget.sendTimes.splice(idx, 1);
      }
      if (stTarget.sendTimes.length > 500) stTarget.sendTimes = stTarget.sendTimes.slice(-500);
      saveSocialV2State();
      sendJson({ ok: false, error: error?.message ?? String(error) }, Number(error?.statusCode) || 500);
    }
  }

  return {
    paths: [RELAY_TARGETS_PATH, RELAY_SEND_PATH],
    /** 返回 true 表示这条请求已被本模块处理（调用方应直接 return）。 */
    async handle(req, res, url) {
      if (req.method === 'GET' && url.pathname === RELAY_TARGETS_PATH) {
        await handleTargets(req, res, url);
        return true;
      }
      if (req.method === 'POST' && url.pathname === RELAY_SEND_PATH) {
        await handleSend(req, res);
        return true;
      }
      return false;
    }
  };
}
