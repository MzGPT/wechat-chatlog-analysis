// 测试前端渲染逻辑
const testMessage = {
    "id": 190,
    "content": null,
    "content_text": "能源开采【6】！！\n能源开采【6】！！\n能源开采【6】！！\n\n[红包]【国海能源开采】煤炭专题研究系列会议",
    "derived": {
        "key_info": "进门:065971 | ai: 国海能源开采举办电解铝行业投资机会专题会议，介绍煤炭专题研究系列会议回顾。"
    },
    "sender_name": "李畅@信达策略",
    "talker_name": "信达研究❤️南方基金投研干货群"
};

// 模拟前端逻辑
function testContentExtraction(m) {
    console.log("=== 测试消息内容提取 ===");
    console.log("m.content:", m.content);
    console.log("m.content_text:", m.content_text);
    
    // 这是前端代码中的逻辑
    let content = m.content || m.content_text || '';
    console.log("提取的content:", content);
    console.log("content长度:", content.length);
    console.log("content前100字符:", content.slice(0, 100));
    
    return content;
}

function testKeyInfoExtraction(meta) {
    console.log("\n=== 测试key_info提取 ===");
    console.log("meta.derived:", meta.derived);
    console.log("meta.derived.key_info:", meta.derived ? meta.derived.key_info : 'undefined');
    
    // 这是updateSummaryCell中的逻辑
    let candidate = (meta && meta.derived && meta.derived.key_info) ? meta.derived.key_info : '';
    console.log("提取的candidate:", candidate);
    
    return candidate;
}

console.log("开始测试...");
const content = testContentExtraction(testMessage);
const keyInfo = testKeyInfoExtraction(testMessage);

console.log("\n=== 最终结果 ===");
console.log("内容是否为空:", content === '');
console.log("key_info是否为空:", keyInfo === '');
