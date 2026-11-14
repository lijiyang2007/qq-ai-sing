// 把「点歌 / 代唱」这两条链路的桥接侧接线**一次性装配好**。
//
// 为什么要有这个文件：
//   bridge.js 里唱歌相关的东西散在 8 个地方（import、服务实例、submit 包装、
//   定时器、工具名映射、两个 HTTP 路由、一个引导文案）。让人照着文档一处一处改，
//   改漏一处就静默失效 —— 而失效方式还特别隐蔽：
//     · 漏了 submitTrackedSong → 歌能唱出来，但永远不主动通知，模型只能说「不知道」
//     · 漏了 relaySongScope   → 代唱能提交，但成品会被 delivery 拒发（拿不到授权）
//     · 漏了 relayTargets 映射 → 工具在 MCP 清单里，但调用永远 403
//
// 所以这里把「装配」写成代码，bridge.js 里只需要：
//
//   1. import { createSingWiring } from './sing-wiring.js';
//   2. 在 startConsoleServer() 里（已有 sendJson/readBody/agentTokenOk 等闭包的地方）：
//        const sing = createSingWiring({ ...deps });
//   3. 在路由分发最前面（其它 /api/socialV2/* 之前）插一行：
//        if (await sing.handle(req, res, url)) return;
//   4. qq_sing / qq_sing_status 的分支改用 sing.submitSong / sing.statusSong。
//
// 详细步骤与 deps 清单见 docs/BRIDGE_WIRING.md。

import { createCoverJobService } from './cover-job.js';
import { createRelaySongScope } from './relay-song-scope.js';
import { createSongIntentService, parseTitleOnlySongRequest } from './song-intent.js';
import { createSongJobNotifier } from './song-job-notifier.js';
import { createVoiceDeliveryReceipts } from './voice-delivery-receipts.js';
import { createRelayRoutes } from './relay-endpoints.js';
import { createSingRoutes } from './sing-endpoints.js';
import {
  parseBilibiliSongRequest, parseBilibiliSongMessage, parseRelayBilibiliSong,
  bilibiliRelayGuidance
} from './bilibili-song.js';
import { parseProSongRequest, parseProSongMessage } from './pro-song.js';

// 把 bridge.js 需要的那几个解析函数从这里统一再导出一次 —— 这样接线时
// 只需要 import 这一个模块，不会漏掉某一个。
export {
  parseProSongRequest, parseProSongMessage,
  parseBilibiliSongRequest, parseBilibiliSongMessage, parseRelayBilibiliSong, bilibiliRelayGuidance,
  parseTitleOnlySongRequest
};

/**
 * 装配点歌 + 代唱。
 *
 * @param {object} deps bridge.js 内部闭包，逐项说明见 docs/BRIDGE_WIRING.md「deps 清单」
 */
export function createSingWiring(deps) {
  const {
    getConfig, stateDir, log = () => {},
    sendJson, readBody,
    agentTokenOk, v2ToolEnabled, v2SessionAllowed, modeAllowed, canonicalV2Key,
    isSessionAllowedInCurrentMode, shouldBlockSilentReply,
    getCurrentMode, isPaused,
    sendToQQ, lastSendSucceeded, recordSentMessagesV2, verifyVoiceMessageSent,
    sendMessagesV2, sendStickerV2, computeGapsV2, findMessageMedia, resolveReplyTargetV2,
    getSocialV2State, saveSocialV2State, scheduleReplyCheckV2, appendActivity,
    sendImageBufferToKey, imageSendRoots, isInsideAllowedImageDir, imageSendMaxPerHour,
    safeFetchBuffer,
    MAX_MEDIA_BYTES, SENSITIVE_RE, KNOWN_AGENT_TOKENS
  } = deps;

  const cfgOf = () => getConfig();

  // ── 语音发送回执：防重复发，且能回读核实 ────────────────────────────────
  const voiceReceipts = createVoiceDeliveryReceipts({ stateDir });
  async function readVoiceReceipt(binding) {
    let record = voiceReceipts.get(binding);
    if (record?.messageId && record.status !== 'verified') {
      const check = await verifyVoiceMessageSent(record.messageId);
      if (check?.confirmed) record = voiceReceipts.markVerified(binding.deliveryId, { messageId: record.messageId });
    }
    return record;
  }

  // ── 代唱授权范围 ────────────────────────────────────────────────────────
  const relaySongScope = createRelaySongScope({
    getConfig: cfgOf, fetchAction: deps.fetchAction, stateDir
  });

  // ── 任务提交服务 ────────────────────────────────────────────────────────
  const coverJobs = createCoverJobService({ getConfig: cfgOf, stateDir, log });

  // ── 主动通知（跑完 / 失败 / 需复核时回原会话说一声）──────────────────────
  const songNotifyAllowed = (key) => getCurrentMode() === 'reserved2' && !isPaused()
    && v2ToolEnabled('sing') && cfgOf()?.socialV2?.sing?.enabled !== false
    && v2SessionAllowed(key) && !shouldBlockSilentReply(key);
  const songNotify = async ({ key, message }) => {
    await sendToQQ(key, message);
    if (!lastSendSucceeded(key)) throw new Error('点歌通知未确认');
    recordSentMessagesV2(key, [message]);
  };
  const songJobNotifier = createSongJobNotifier({
    stateDir, readStatus: (args) => coverJobs.status(args),
    allowed: songNotifyAllowed, notify: songNotify, log
  });

  /**
   * 提交并登记主动通知。
   *
   * 这个包装**不能省**：coverJobs.submit 只负责建任务，通知要靠 notifier 登记。
   * 直接调 submit 的话，任务照样会做完、也照样会发语音，但「失败/需复核」这类
   * 需要主动说一声的终态永远不会通知到会话 —— 表现就是模型被问到进度时只能说不知道。
   */
  const submitTrackedSong = async (args) => {
    const result = await coverJobs.submit(args);
    if (result?.job) {
      try {
        songJobNotifier.track({
          job: result.job, key: args.target, song: result.song || args.song,
          ...(args.notifyKey && args.notifyKey !== args.target ? { notifyKey: args.notifyKey } : {})
        });
      } catch (e) {
        log('[sing-notify] tracking failed', e?.message ?? e);
        result.notificationWarning = '任务已提交，但主动通知登记失败；请用任务号查进度，不要重复提交。';
      }
    }
    return result;
  };

  const songJobTimer = setInterval(
    () => songJobNotifier.tick().catch((e) => log('[sing-notify] check failed', e?.message ?? e)), 5000);
  songJobTimer.unref?.();

  // ── 歌名解析（含「等歌手 / 补充歌手 / 更换 / 取消」这类多轮交互）────────
  const songIntents = createSongIntentService({
    stateDir,
    allowed: (key) => getCurrentMode() === 'reserved2' && !isPaused()
      && v2ToolEnabled('sing') && cfgOf()?.socialV2?.sing?.enabled !== false
      && v2SessionAllowed(key) && !shouldBlockSilentReply(key),
    resolve: (title, artist, autoSelect) => {
      const video = parseBilibiliSongRequest(title);
      if (video) return { song: 'B站视频 ' + video.url, title: '指定B站视频音轨', artist: '指定视频', videoUrl: video.url };
      const pro = parseProSongRequest(title);
      return pro ? coverJobs.resolveSong({ proQuery: pro.query })
        : autoSelect && !artist
          ? coverJobs.resolveSong({ autoOriginalQuery: title })
          : coverJobs.resolveSong({ title, artist, autoSelect });
    },
    submit: ({ song, trackId, key, mode, allowVariant, videoUrl }) => submitTrackedSong({
      song, target: key, mode, ...(videoUrl ? { videoUrl } : { trackId }), ...(allowVariant ? { allowVariant: true } : {})
    }),
    notify: async ({ key, message }) => {
      await sendToQQ(key, message);
      if (!lastSendSucceeded(key)) throw new Error('QQ通知结果未确认');
      recordSentMessagesV2(key, [message]);
    },
    log
  });
  const songIntentTimer = setInterval(
    () => { songIntents.tick().catch((e) => log('[sing-wait] tick failed', e?.message ?? e)); }, 1000);
  songIntentTimer.unref?.();

  // ── 代唱引导：识别「管理员让机器人唱给别人」这种请求 ─────────────────────
  const relayGuidance = (key, messages) => bilibiliRelayGuidance({
    key, ownerQQ: cfgOf()?.ownerQQ, messages, allow: cfgOf()?.allow,
    aliases: cfgOf()?.socialV2?.aliases,
    enabled: getCurrentMode() === 'reserved2' && v2ToolEnabled('relaySend') && v2ToolEnabled('sing')
  });

  // ── 代发 / 代唱的两条 HTTP 路由 ─────────────────────────────────────────
  const relayRoutes = createRelayRoutes({
    getConfig: cfgOf, path: deps.path, fs: deps.fs, log, appendActivity,
    sendJson, readBody, agentTokenOk, v2ToolEnabled, modeAllowed, canonicalV2Key,
    isSessionAllowedInCurrentMode, shouldBlockSilentReply, getCurrentMode, isPaused,
    coverJobs, relaySongScope, submitTrackedSong,
    imageSendRoots, isInsideAllowedImageDir, imageSendMaxPerHour,
    sendImageBufferToKey, sendMessagesV2, sendStickerV2, computeGapsV2,
    findMessageMedia, resolveReplyTargetV2,
    getSocialV2State, saveSocialV2State, recordSentMessagesV2, scheduleReplyCheckV2,
    MAX_MEDIA_BYTES, SENSITIVE_RE, KNOWN_AGENT_TOKENS, safeFetchBuffer
  });

  // ── 点歌本身的两条 HTTP 路由（/api/socialV2/sing、sing-status）────────────
  const singRoutes = createSingRoutes({
    getConfig: cfgOf, sendJson, readBody, log,
    agentTokenOk, v2ToolEnabled, v2SessionAllowed, modeAllowed,
    shouldBlockSilentReply, getCurrentMode, isPaused,
    coverJobs, songIntents, submitTrackedSong, relayGuidance,
    parseBilibiliSongRequest, parseProSongRequest, getSocialV2State
  });

  return {
    coverJobs, relaySongScope, songJobNotifier, songIntents, voiceReceipts,
    submitTrackedSong, relayGuidance, readVoiceReceipt,
    parseProSongMessage,    /** qq_sing 的提交入口（route 里已经做完 key/token/白名单校验后调它） */
    submitSong: submitTrackedSong,
    /** qq_sing_status 的查询入口 */
    statusSong: (args) => coverJobs.status(args),
    /** 路由分发：返回 true 表示已处理。两条链路的路径都在这里一次拦下。 */
    async handle(req, res, url) {
      if (await relayRoutes.handle(req, res, url)) return true;
      return singRoutes.handle(req, res, url);
    },
    /** 关服务器时清掉定时器，避免测试/热重载泄漏 */
    close() {
      clearInterval(songJobTimer);
      clearInterval(songIntentTimer);
    }
  };
}
