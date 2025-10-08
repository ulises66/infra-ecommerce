FROM public.ecr.aws/docker/library/python:3.11-alpine
WORKDIR /app
COPY index.html /app/index.html
EXPOSE 3000
CMD ["python", "-m", "http.server", "3000"]
