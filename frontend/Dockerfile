# ---- стадия сборки ----
FROM node:22-slim AS videocutter-ui-build

WORKDIR /app

COPY package.json package-lock.json* ./
RUN npm install

COPY . .
RUN npm run build

# ---- стадия раздачи ----
FROM nginx:1.27-alpine

COPY default.conf /etc/nginx/conf.d/default.conf
COPY --from=videocutter-ui-build /app/dist /usr/share/nginx/html

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]