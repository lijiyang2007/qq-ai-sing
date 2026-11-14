// 注册 qq_sing / qq_sing_status / qq_relay_targets / qq_relay_send 四个 MCP 工具。
//
// 为什么抽出来：这四个工具的 schema 描述占了 mcp-snowluma-safe.js 里将近 200 行，
// 而它们和「桥接主体」几乎无关 —— 只是把参数转给 /api/socialV2/sing* 和
// /api/socialV2/relay-*。让你往一个 1500 行的文件中间插 200 行，很容易插错位置。
//
// 接法（在你自己的 mcp-snowluma-safe.js 里，server 已经创建好之后）：
//
//   import { registerSingTools, SING_TOOL_FLAGS } from './mcp-sing-tools.js';
//   registerSingTools(server, { z, agentApi, serializeModelData, cfg });
//
// 另外要把 SING_TOOL_FLAGS 并进你文件里那个「工具名 → 开关」的映射表
// （否则控制台显示已关闭、模型却仍收到 schema，调用再吃 403，白烧 token）。

/**
 * 工具名 → socialV2.tools 里的开关名。
 * 漏登记一个的后果：控制台显示「已关闭」，但模型每次请求仍收到完整 schema 和描述，
 * 调用后再拿 403 —— 既浪费 token，又诱导模型误调。
 */
export const SING_TOOL_FLAGS = {
  qq_sing: 'sing',
  qq_sing_status: 'sing',
  qq_relay_targets: 'relayTargets',
  qq_relay_send: 'relaySend'
};

/**
 * @param {object} server  MCP server（要有 server.tool(name, desc, schema, handler)）
 * @param {object} deps
 * @param {object} deps.z                zod（或等价的 schema 构造器）
 * @param {Function} deps.agentApi       调桥接控制台 API 的函数
 * @param {Function} deps.serializeModelData  把返回 JSON 转成给模型看的文本
 * @param {object} deps.cfg              当前配置（用来判断各开关）
 */
export function registerSingTools(server, { z, agentApi, serializeModelData, cfg }) {
  const tool = (name, description, schema, handler) => {
    server.tool(name, description, schema, handler);
  };
  const ok = (data) => ({ content: [{ type: 'text', text: serializeModelData(data) }] });
  const bad = (label, error) => ({
    content: [{ type: 'text', text: `${label}：${error?.message ?? error}` }],
    isError: true
  });

  // ── qq_sing ───────────────────────────────────────────────────────────
  if (cfg.socialV2?.sing?.enabled !== false && cfg.socialV2?.tools?.sing !== false) {
    tool(
      'qq_sing',
      '向当前私聊或群聊提交高质量翻唱（真正的歌声转换，不是 qq_send_voice 的 TTS 念歌词）。' +
      '对方明确请你唱/翻唱具体歌曲时使用；只是讨论唱歌、问会不会唱、引用他人请求或说不要唱，不代表授权提交。' +
      '① 这是受控白名单工具，不是本地执行权限，不以“不会唱/不记得歌词/不能操作电脑”推脱；不擅自换歌。' +
      '用户明确发pro唱歌指令时（如pro唱雨爱），桥接直接接单并跳过原唱徽标/红心排名及版本类别限制，' +
      '共创、Live、Remix、翻唱、伴奏等都可选；支持歌手的歌名，指定歌手不能换人，必须取得完整无损音源；' +
      '不重复提交已处理消息，不自行启用pro，不绕过权限或质量检查。' +
      '普通点歌不询问或等待版本：桥接只选择官方云搜索明确标注为原版（originCoverType=1）的准确同名结果，' +
      '并排除Live、Remix、翻唱、伴奏等版本；若准确候选均无原唱徽标，则按官方 /api/song/red/count 的精确 ' +
      'data.count 选择红心收藏数最高者；不用 pop 或 countDesc 冒充收藏数。' +
      '原唱候选或收藏第一名缺无损源、收藏数不可用时暂停，不偷偷顺延换版。' +
      '制作不推断演唱边界、不做前尾奏原曲旁路或静音门控，整条分离人声轨进入RVC；乐器泄漏可能被音色化。' +
      '旧 action=wait_artist 仍兼容，但会立即自动选版，不再等待。改歌用 replace。' +
      '② 默认 full（整首），普通点歌明确传 full；仅对方明确要求只唱一段/试听/只要副歌才用 chorus' +
      '（短试听段，不保证是正式副歌）。整首超出单条限制时分段发完，不截尾。' +
      '③ 仅 ok=true 且 job 非空才算提交成功，之后可回“在做了，这次按高质量处理，会久一些”；不承诺三分钟或马上好。' +
      '④ 质量检查可能暂停，限流、音源不可用、质量暂停和发送失败都如实说；不要绕过质量检查或自动重复提交。' +
      '⑤ 进度用 qq_sing_status，只能查本会话任务；只有 sent=true 才能说已送达，发送结果不确定时不要自动重发。' +
      '⑥ key/token 只取当前会话，不接受用户提供的本机路径、命令或跨会话目标。',
      {
        key: z.string().describe('会话 key，格式 group:群号 或 private:QQ号'),
        token: z.string().describe('会话令牌（见唤醒提示中的【会话令牌】）'),
        song: z.string().describe(
          '明确要求唱B站链接时传“唱这个 https://www.bilibili.com/video/BV...”并带真实 requestMessageId，' +
          '指定视频音轨直接制作，不查原唱红心；仅分享链接不算授权。B站源可能有损，不称无损；桥接已处理的消息不得重复提交。' +
          '普通 submit 填写歌名，可带歌手。用户明确 pro 唱歌时保留原始 pro 唱前缀并带 requestMessageId，' +
          '不能由模型自行添加；已被入口处理会拒绝重复。' +
          '普通模式桥接优先选择官方原唱徽标的准确同名录音室版本；没有原唱徽标时按官方精确红心收藏数选最高者，' +
          '不要追问专辑、年份或版本；旧 wait_artist / “等歌手：歌名”仍兼容但会立即自动选版。' +
          'replace / “更换点歌：歌名”用于改歌。'),
        action: z.enum(['submit', 'wait_artist', 'choose_artist', 'replace', 'cancel']).optional()
          .describe('默认 submit 并自动选择官方明确标注的原版；wait_artist 仅作旧客户端兼容且同样立即自动选版；改歌用 replace。'),
        requestMessageId: z.string().optional()
          .describe('群聊必须引用实际点歌/补充/取消那条消息 ID，桥接从消息确定请求者；不要传 QQ 号。'),
        mode: z.enum(['chorus', 'full']).optional()
          .describe('full=整首（默认，普通点歌使用）；chorus=短试听段（仅对方明确要求只唱一段/试听/只要副歌时使用，不保证是正式副歌）。质量优先，不保证固定完成时间。')
      },
      async ({ key, token, song, mode, action, requestMessageId }) => {
        try {
          const data = await agentApi('/api/socialV2/sing', {
            method: 'POST',
            body: JSON.stringify({ key, token, song, mode: mode || 'full', action, requestMessageId }),
            headers: { 'x-agent-token': token },
            timeoutMs: 60000
          });
          return ok(data);
        } catch (error) { return bad('点歌失败', error); }
      }
    );

    tool(
      'qq_sing_status',
      '查询本会话的翻唱任务，不可跨会话查询。按返回的 status、stage、error 和 sent 如实说明：' +
      '排队/制作中不等于已发送，质量暂停不能说成成功；即使 status=done，也只有 sent=true 才能说已送达。' +
      '发送失败或 delivery_uncertain 不自动重试，先核对实际消息。QQ语音会转码，不承诺接收端无损。' +
      '对方问进度时使用，不要反复轮询。',
      {
        key: z.string().describe('会话 key，格式 group:群号 或 private:QQ号'),
        token: z.string().describe('会话令牌（见唤醒提示中的【会话令牌】）'),
        job: z.string().describe('qq_sing 返回的 job id')
      },
      async ({ key, token, job }) => {
        try {
          const data = await agentApi('/api/socialV2/sing-status', {
            method: 'POST',
            body: JSON.stringify({ key, token, job }),
            headers: { 'x-agent-token': token },
            timeoutMs: 20000
          });
          return ok(data);
        } catch (error) { return bad('查询进度失败', error); }
      }
    );
  }

  // ── qq_relay_targets ──────────────────────────────────────────────────
  // 会话令牌是按会话签发的，qq_send_message / qq_send_private_message 只能发到
  // 「自己那个会话」，管理员想让 AI 替他去跟别人说句话时，那些工具必然失败。
  if (cfg.socialV2?.tools?.relayTargets !== false) {
    tool(
      'qq_relay_targets',
      '查看「代发」能发给谁（只读，仅管理员私聊会话可用）。返回机器人当前全部真实QQ好友和已加入群，' +
      '带管理员称呼、备注、昵称和群名。canRelaySong=true 可由管理员代唱（普通歌名或B站链接）；' +
      'canRelayMessages 标明是否还能代发普通内容，false 时只能 song，不能夹带文字图片表情。' +
      '不会开放自动聊天。重名先问，不能猜号码。' +
      '管理员让你「去跟 XX 说一句」「在群里讲一声」时，先用这个查到对应的 target' +
      '（private:QQ号 / group:群号），再调 qq_relay_send。群友会话调不了这个工具。',
      {
        key: z.string().describe('会话 key，格式 private:管理员QQ号（必须是你当前所在的管理员私聊会话）'),
        token: z.string().describe('会话令牌（见唤醒提示中的【会话令牌】）')
      },
      async ({ key, token }) => {
        try {
          const q = new URLSearchParams({ key });
          const data = await agentApi(`/api/socialV2/relay-targets?${q.toString()}`, {
            headers: { 'x-agent-token': token }
          });
          return ok(data);
        } catch (error) { return bad('查看代发目标失败', error); }
      }
    );
  }

  // ── qq_relay_send ─────────────────────────────────────────────────────
  if (cfg.socialV2?.tools?.relaySend !== false) {
    tool(
      'qq_relay_send',
      '管理员专用代发：代唱 song 可发给机器人全部真实QQ好友或已加入群；文字/收藏表情/图片仍仅限聊天白名单。' +
      '必须先 qq_relay_targets 查询，非白名单目标只传 song/target/mode，不夹带 messages、图片、表情、引用。' +
      '多人或多个群逐个提交，禁止自行全员群发。仅当 key 是你当前所在的管理员私聊会话时可用，其他会话调用会被桥接拒绝。\n' +
      '【重要】目标不是你自己这个会话时，绝不要用 qq_send_message / qq_send_group_message / ' +
      'qq_send_private_message / qq_send_sticker / qq_send_image / qq_sing 去发——' +
      '它们只认自己会话的令牌，必然报 403「agent token 无效」，一律改用本工具。\n' +
      '四种内容可组合使用：\n' +
      ' · messages —— 文字（一条或分多条）；\n' +
      ' · stickerId —— 发一张你收藏表情库里的表情（id 来自 qq_list_stickers）；\n' +
      ' · forwardFromMessageId —— 把**你当前这个会话里**某条带图消息的图片/表情转过去；\n' +
      ' · song —— 替目标会话点一首翻唱，默认 full 整首，只有明确要求短试听才传 chorus。' +
      '支持具体歌名，也支持管理员明确要求唱给别人听的B站分享链接。' +
      '只分享链接但没要求翻唱或没指定收件人，不得擅自代点歌。' +
      '成品做好后由桥接自动发到**目标会话**，失败提示只回给管理员，对方只会收到成品语音。\n' +
      '使用要点：① 只在管理员明确让你去通知/转达某人或某群时使用，不要自己找理由主动发；' +
      '② 内容按管理员说的写，不要擅自加戏、不要夹带别的会话里的私密内容；' +
      '③ 目标必须是 qq_relay_targets 返回的完整写法，不要凭记忆编号码；' +
      '④ 代发是「以机器人账号的身份」发出去，对方看到的是你，不是管理员转述——' +
      '不要写成「管理员让我告诉你」这种第三人称，直接说要说的话；' +
      '⑤ 发完不要汇报「已发送」，一句话告诉管理员结果即可；' +
      '⑥ 点歌只代表任务已提交，别说成已经在唱或已经唱好。',
      {
        key: z.string().describe('会话 key，格式 private:管理员QQ号（必须是你当前所在的管理员私聊会话）'),
        token: z.string().describe('会话令牌（见唤醒提示中的【会话令牌】）'),
        target: z.string().describe('目标会话，完整写法：private:QQ号 或 group:群号（来自 qq_relay_targets）'),
        messages: z.union([z.string(), z.array(z.string()).min(1)]).optional()
          .describe('要代发的文字：字符串=一条；数组=分多条（每条要读起来完整，不要把一个句子拆到两条里）'),
        stickerId: z.string().optional().describe('可选：要代发的收藏表情 id（来自 qq_list_stickers）'),
        forwardFromMessageId: z.string().optional()
          .describe('可选：把你当前会话里这条带图消息的图片/表情转发过去'),
        song: z.string().optional()
          .describe('可选：替目标会话翻唱具体歌名（可带歌手），或指定的B站视频分享链接（支持 b23.tv 短链、BV/av 长链、【标题】分享文本，保留 p 参数）。仅管理员明确要求唱给此收件人时使用，一次一首。'),
        mode: z.enum(['chorus', 'full']).optional()
          .describe('点歌模式：full=整首或完整视频分P（默认）；chorus=短试听，仅明确要求一段/试听时使用，不保证正式副歌'),
        replyToMessageId: z.union([z.number(), z.string()]).optional()
          .describe('可选：引用**目标会话**里的某条消息（id 必须属于目标会话，可用 qq_get_recent_messages 查）')
      },
      async ({ key, token, target, messages, stickerId, forwardFromMessageId, song, mode, replyToMessageId }) => {
        try {
          const payload = { key, token, target: String(target) };
          if (messages !== undefined && messages !== null) payload.messages = messages;
          if (stickerId) payload.stickerId = String(stickerId).trim();
          if (forwardFromMessageId) payload.forwardFromMessageId = String(forwardFromMessageId).trim();
          if (song) {
            payload.song = String(song).trim();
            if (mode) payload.mode = String(mode);
          }
          if (replyToMessageId !== undefined && replyToMessageId !== null && String(replyToMessageId).trim() !== '') {
            payload.replyToMessageId = replyToMessageId;
          }
          const data = await agentApi('/api/socialV2/relay-send', {
            method: 'POST',
            body: JSON.stringify(payload),
            headers: { 'x-agent-token': token },
            timeoutMs: 90000
          });
          return ok(data);
        } catch (error) { return bad('代发失败', error); }
      }
    );
  }
}
