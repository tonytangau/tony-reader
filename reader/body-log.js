const RAW=[{"date":"2026-06-06","weight":93.8,"bodyFat":27.1},{"date":"2026-06-07","weight":93.5,"bodyFat":27},{"date":"2026-06-08","weight":93,"bodyFat":26.8},{"date":"2026-06-09","weight":92.5,"bodyFat":26.7},{"date":"2026-06-10","weight":92.75,"bodyFat":26.4},{"date":"2026-06-11","weight":92.2,"bodyFat":26.3},{"date":"2026-06-12","weight":91.9,"bodyFat":null},{"date":"2026-06-13","weight":92.15,"bodyFat":25.8},{"date":"2026-06-14","weight":91.85,"bodyFat":25.7},{"date":"2026-06-15","weight":92.2,"bodyFat":25.6},{"date":"2026-06-16","weight":92.2,"bodyFat":25.4},{"date":"2026-06-17","weight":91.85,"bodyFat":25.6},{"date":"2026-06-19","weight":92.6,"bodyFat":25.2},{"date":"2026-06-29","weight":91.45,"bodyFat":24.2},{"date":"2026-06-30","weight":91.1,"bodyFat":25.1},{"date":"2026-07-01","weight":90.4,"bodyFat":25.3},{"date":"2026-07-02","weight":90.75,"bodyFat":25.3},{"date":"2026-07-03","weight":90.95,"bodyFat":25.1},{"date":"2026-07-04","weight":90.9,"bodyFat":24.9},{"date":"2026-07-05","weight":91.35,"bodyFat":24.6},{"date":"2026-07-12","weight":91.85,"bodyFat":24.3},{"date":"2026-07-13","weight":91.9,"bodyFat":24.3},{"date":"2026-07-14","weight":91.15,"bodyFat":24.3},{"date":"2026-07-15","weight":91.05,"bodyFat":24.2},{"date":"2026-07-16","weight":90.7,"bodyFat":24.2},{"date":"2026-07-18","weight":91.5,"bodyFat":24},{"date":"2026-07-19","weight":92.45,"bodyFat":24},{"date":"2026-07-20","weight":91.7,"bodyFat":24},{"date":"2026-07-21","weight":91.2,"bodyFat":24.2},{"date":"2026-07-22","weight":91.05,"bodyFat":24.5},{"date":"2026-07-23","weight":90.3,"bodyFat":24.9},{"date":"2026-07-24","weight":90.4,"bodyFat":25.2},{"date":"2026-07-25","weight":90.9,"bodyFat":25.2},{"date":"2026-07-26","weight":90.7,"bodyFat":25},{"date":"2026-07-28","weight":90.1,"bodyFat":24.6},{"date":"2026-07-29","weight":90.1,"bodyFat":24.6},{"date":"2026-07-30","weight":89.9,"bodyFat":24.4},{"date":"2026-07-31","weight":90,"bodyFat":24.2},{"date":"2026-08-01","weight":89.8,"bodyFat":24.1},{"date":"2026-08-02","weight":89.45,"bodyFat":24.4},{"date":"2026-08-03","weight":89.6,"bodyFat":24.4},{"date":"2026-08-04","weight":90.15,"bodyFat":24},{"date":"2026-08-05","weight":89.6,"bodyFat":23.9},{"date":"2026-08-06","weight":89.05,"bodyFat":23.8},{"date":"2026-08-07","weight":89,"bodyFat":23.6},{"date":"2026-08-08","weight":88.75,"bodyFat":23.5}];

var start=new Date("2026-06-05");
var days=RAW.map(function(d){var dt=new Date(d.date);return{i:Math.round((dt-start)/86400000),label:d.date.slice(5),w:d.weight,bf:d.bodyFat}});
var maxI=days[days.length-1].i;

function linreg(a){var n=a.length,sx=0,sy=0,sxy=0,sx2=0;for(var i=0;i<n;i++){var p=a[i];sx+=p.x;sy+=p.y;sxy+=p.x*p.y;sx2+=p.x*p.x}var m=(n*sxy-sx*sy)/(n*sx2-sx*sx),b=(sy-m*sx)/n,ym=sy/n,ssr=0,sst=0;for(var i=0;i<n;i++){var p=a[i],f=m*p.x+b;ssr+=(p.y-f)*(p.y-f);sst+=(p.y-ym)*(p.y-ym)}return{m:m,b:b,r2:1-ssr/sst}}

var wReg=linreg(days.map(function(d){return{x:d.i,y:d.w}}));

function draw(cid,field,color,yMin,yMax,label,showTrend){
  var c=document.getElementById(cid),dpr=window.devicePixelRatio||1,W=c.parentElement.clientWidth-32,H=300;
  c.width=W*dpr;c.height=H*dpr;c.style.width=W+'px';c.style.height=H+'px';
  var ctx=c.getContext('2d');ctx.scale(dpr,dpr);
  var pad={t:28,r:20,b:40,l:52},pw=W-pad.l-pad.r,ph=H-pad.t-pad.b;
  function x(i){return pad.l+(i/maxI)*pw}
  function y(v){return pad.t+ph-((v-yMin)/(yMax-yMin))*ph}

  ctx.fillStyle='#1e293b';ctx.fillRect(0,0,W,H);

  // grid
  ctx.strokeStyle='#334155';ctx.lineWidth=1;
  for(var v=yMin;v<=yMax;v+=(yMax-yMin)/5){
    var yy=y(v);
    ctx.beginPath();ctx.moveTo(pad.l,yy);ctx.lineTo(W-pad.r,yy);ctx.stroke();
    ctx.fillStyle='#64748b';ctx.font='10px -apple-system,sans-serif';ctx.textAlign='right';
    ctx.fillText(v.toFixed(1),pad.l-8,yy+4);
  }

  // month labels
  ctx.fillStyle='#64748b';ctx.textAlign='center';
  for(var m=0;m<=2;m++){
    var d=new Date(2026,5+m,1),xi=x(Math.round((d-start)/86400000));
    ctx.fillText(['Jun','Jul','Aug'][m],xi,H-pad.b+16);
  }

  // trend line
  if(showTrend){
    ctx.strokeStyle='#f59e0b';ctx.lineWidth=1.5;ctx.setLineDash([6,4]);
    ctx.beginPath();ctx.moveTo(x(0),y(wReg.m*0+wReg.b));ctx.lineTo(x(maxI),y(wReg.m*maxI+wReg.b));ctx.stroke();
    ctx.setLineDash([]);
  }

  // filter nulls, build points
  var pts=[];
  for(var i=0;i<days.length;i++){
    var d=days[i];
    if(d[field]!=null) pts.push({xi:x(d.i),yi:y(d[field]),v:d[field],i:d.i});
  }
  if(pts.length<2) return;

  // line + fill
  ctx.beginPath();ctx.moveTo(pts[0].xi,pts[0].yi);
  for(var i=1;i<pts.length;i++){var cp=(pts[i-1].xi+pts[i].xi)/2;ctx.bezierCurveTo(cp,pts[i-1].yi,cp,pts[i].yi,pts[i].xi,pts[i].yi)}
  ctx.strokeStyle=color;ctx.lineWidth=2;ctx.stroke();
  ctx.lineTo(pts[pts.length-1].xi,y(yMin));ctx.lineTo(pts[0].xi,y(yMin));ctx.closePath();
  ctx.fillStyle=color.replace(')','').replace('rgb','rgba').replace('(','(')+',0.1)';ctx.fill();

  // dots + labels
  var step=Math.max(1,Math.floor(pts.length/15));
  for(var i=0;i<pts.length;i+=step){
    ctx.beginPath();ctx.arc(pts[i].xi,pts[i].yi,4,0,Math.PI*2);ctx.fillStyle=color;ctx.fill();
    ctx.fillStyle='#e2e8f0';ctx.font='bold 9px -apple-system,sans-serif';ctx.textAlign='center';
    ctx.fillText(pts[i].v.toFixed(1),pts[i].xi,pts[i].yi-12);
  }
  // always first + last
  for(var j=0;j<2;j++){
    var idx=j===0?0:pts.length-1;
    ctx.fillStyle='#e2e8f0';ctx.font='bold 10px -apple-system,sans-serif';ctx.textAlign='center';
    ctx.fillText(pts[idx].v.toFixed(1),pts[idx].xi,pts[idx].yi-12);
  }

  // legend
  ctx.fillStyle=color;ctx.fillRect(W-170,8,10,10);
  ctx.fillStyle='#cbd5e1';ctx.font='10px -apple-system,sans-serif';ctx.textAlign='left';ctx.fillText(label,W-155,16);
  if(showTrend){
    ctx.fillStyle='#f59e0b';ctx.fillRect(W-95,8,10,10);
    ctx.fillText('趋势 R²='+wReg.r2.toFixed(3),W-80,16);
  }
}

draw('c1','w','rgb(56,189,248)',85,96,'体重 kg',true);
draw('c2','bf','rgb(52,211,153)',22,28,'体脂率 %',false);
