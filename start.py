import UserAgent from 'npm:user-agents@1.1.379';

function generateId() {
  const chars = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';
  let result = '';
  for (let i = 0; i < 16; i++) {
    result += chars.charAt(Math.floor(Math.random() * chars.length));
  }
  return result;
}

function getRandomUA() {
  return new UserAgent().toString();
}

function createHeaders(sessionToken) {
  return {
    'accept': '*/*',
    'accept-language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'content-type': 'application/json',
    'origin': 'https://banana.listenhub.ai',
    'referer': 'https://banana.listenhub.ai/',
    'user-agent': getRandomUA(),
    'cookie': `session=${sessionToken}`
  };
}

function parseModelParams(model) {
  const parts = model.split('-');
  let size = '2k';
  let aspectRatio = '16:9';

  for (const part of parts) {
    if (['1k', '2k', '4k'].includes(part)) {
      size = part;
    } else if (['1:1', '16:9', '9:16', '4:3', '3:4'].includes(part)) {
      aspectRatio = part;
    }
  }

  return { size: size.toUpperCase(), aspectRatio };
}

async function urlToBase64(url) {
  if (url.startsWith('data:')) {
    return url;
  }

  const response = await fetch(url);
  const buffer = await response.arrayBuffer();
  const base64 = btoa(String.fromCharCode(...new Uint8Array(buffer)));
  const contentType = response.headers.get('content-type') || 'image/png';
  return `data:${contentType};base64,${base64}`;
}

function extractUserContent(messages) {
  const lastMessage = messages[messages.length - 1];
  if (lastMessage.role !== 'user') {
    throw new Error('Last message must be from user');
  }

  let text = '';
  let images = [];

  if (typeof lastMessage.content === 'string') {
    text = lastMessage.content;
  } else if (Array.isArray(lastMessage.content)) {
    for (const item of lastMessage.content) {
      if (item.type === 'text') {
        text = item.text;
      } else if (item.type === 'image_url') {
        images.push(item.image_url.url);
      }
    }
  }

  return { text, images };
}

async function pollTaskStatus(taskId, sessionToken, maxAttempts = 60) {
  await new Promise(resolve => setTimeout(resolve, 10000));

  const headers = createHeaders(sessionToken);
  delete headers['content-type'];

  for (let i = 0; i < maxAttempts; i++) {
    const response = await fetch(`https://banana.listenhub.ai/api/images/${taskId}`, { headers });

    if (!response.ok) {
      const errorText = await response.text();
      console.error('Poll error:', response.status, errorText);
      throw new Error(`Poll failed: ${response.status}`);
    }

    const data = await response.json();

    if (data.task.state === 'completed') {
      return data.task.result.imageUrl;
    } else if (data.task.state === 'failed') {
      console.error('Task failed:', data.task.error);
      throw new Error(data.task.error || 'Task failed');
    }

    await new Promise(resolve => setTimeout(resolve, 3000));
  }

  throw new Error('Timeout waiting for image generation');
}

function createStreamResponse(imageUrl, model) {
  const stream = new ReadableStream({
    start(controller) {
      const encoder = new TextEncoder();
      const messageId = 'chatcmpl-' + generateId();
      const content = `![image](${imageUrl})`;

      const chunk = {
        id: messageId,
        object: 'chat.completion.chunk',
        created: Math.floor(Date.now() / 1000),
        model: model,
        choices: [{
          index: 0,
          delta: { content },
          finish_reason: null
        }]
      };
      controller.enqueue(encoder.encode('data: ' + JSON.stringify(chunk) + '\n\n'));

      const finalChunk = {
        id: messageId,
        object: 'chat.completion.chunk',
        created: Math.floor(Date.now() / 1000),
        model: model,
        choices: [{
          index: 0,
          delta: {},
          finish_reason: 'stop'
        }]
      };
      controller.enqueue(encoder.encode('data: ' + JSON.stringify(finalChunk) + '\n\n'));
      controller.enqueue(encoder.encode('data: [DONE]\n\n'));
      controller.close();
    }
  });

  return new Response(stream, {
    headers: {
      'Content-Type': 'text/event-stream',
      'Cache-Control': 'no-cache',
      'Connection': 'keep-alive'
    }
  });
}

function createNonStreamResponse(imageUrl, model) {
  return new Response(JSON.stringify({
    id: 'chatcmpl-' + generateId(),
    object: 'chat.completion',
    created: Math.floor(Date.now() / 1000),
    model: model,
    choices: [{
      index: 0,
      message: {
        role: 'assistant',
        content: `![image](${imageUrl})`
      },
      finish_reason: 'stop'
    }],
    usage: {
      prompt_tokens: 0,
      completion_tokens: 0,
      total_tokens: 0
    }
  }), {
    headers: { 'Content-Type': 'application/json' }
  });
}

function createErrorResponse(message, status = 500) {
  return new Response(JSON.stringify({
    error: {
      message: message,
      type: 'server_error',
      code: status
    }
  }), {
    status: status,
    headers: { 'Content-Type': 'application/json' }
  });
}

Deno.serve(async (req) => {
  const url = new URL(req.url);

  if (url.pathname === '/v1/models' && req.method === 'GET') {
    const sizes = ['1k', '2k', '4k'];
    const ratios = ['1:1', '16:9', '9:16', '4:3', '3:4'];
    const models = [];

    for (const size of sizes) {
      for (const ratio of ratios) {
        models.push({
          id: `gemini-3-pro-image-preview-${size}-${ratio}`,
          object: 'model',
          created: 1677610602,
          owned_by: 'banana'
        });
      }
    }

    return new Response(JSON.stringify({
      object: 'list',
      data: models
    }), {
      headers: { 'Content-Type': 'application/json' }
    });
  }

  if (url.pathname === '/v1/chat/completions' && req.method === 'POST') {
    const authHeader = req.headers.get('Authorization');
    if (!authHeader || !authHeader.startsWith('Bearer ')) {
      return createErrorResponse('Missing or invalid Authorization header', 401);
    }

    const sessionToken = authHeader.substring(7);

    let openaiRequest;
    try {
      openaiRequest = await req.json();
    } catch {
      return createErrorResponse('Invalid JSON', 400);
    }

    try {
      const { text, images } = extractUserContent(openaiRequest.messages);
      const { size, aspectRatio } = parseModelParams(openaiRequest.model || 'gemini-3-pro-image-preview');

      const payload = {
        prompt: text,
        mode: images.length > 0 ? 'edit' : 't2i',
        visibility: 'private',
        params: { size, aspectRatio }
      };

      if (images.length > 0) {
        payload.images = await Promise.all(images.map(url => urlToBase64(url)));
      }

      console.log('Upstream payload:', JSON.stringify({
        ...payload,
        images: payload.images ? `[${payload.images.length} images, base64 omitted]` : undefined
      }, null, 2));

      let response = await fetch('https://banana.listenhub.ai/api/images/generate', {
        method: 'POST',
        headers: createHeaders(sessionToken),
        body: JSON.stringify(payload)
      });

      if (!response.ok && response.status === 500) {
        const errorText = await response.text();
        console.error('Upstream error (retrying with public):', response.status, errorText);

        payload.visibility = 'public';
        response = await fetch('https://banana.listenhub.ai/api/images/generate', {
          method: 'POST',
          headers: createHeaders(sessionToken),
          body: JSON.stringify(payload)
        });
      }

      if (!response.ok) {
        const errorText = await response.text();
        console.error('Upstream error:', response.status, errorText);
        return createErrorResponse('Upstream request failed', response.status);
      }

      const { taskId } = await response.json();
      const imageUrl = await pollTaskStatus(taskId, sessionToken);

      const isStreamRequested = openaiRequest.stream === true;
      return isStreamRequested
        ? createStreamResponse(imageUrl, openaiRequest.model || 'gemini-3-pro-image-preview')
        : createNonStreamResponse(imageUrl, openaiRequest.model || 'gemini-3-pro-image-preview');

    } catch (error) {
      if (error.message === 'Timeout waiting for image generation') {
        return createErrorResponse('Timeout waiting for image generation', 500);
      }
      return createErrorResponse(error.message, 500);
    }
  }

  return new Response('Not Found', { status: 404 });
});
