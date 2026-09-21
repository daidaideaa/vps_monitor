// 本地模拟商家；运行时测试不访问外网，也不触发真实库存检查。
export default {
  fetch(request) {
    if (request.url.includes('runtime-redirect=1')) {
      return Response.redirect('https://merchant.test/final', 302);
    }
    return new Response(request.url.includes('zgovps') ?
      '<h1>Tokyo Intel VPS</h1><div>Starter Out of stock!</div><div>Standard Continue</div>' :
      '<div>JP2-CO-Micro-Lite 0 Available JP2-CO-Mini-Lite 2 Available</div>',
    { headers: { 'Content-Type': 'text/html' } });
  },
};
