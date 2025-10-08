FROM public.ecr.aws/docker/library/python:3.11-alpine
WORKDIR /app
COPY server.py /app/server.py
EXPOSE 4000
ENV PORT=4000
CMD ["python", "server.py"]
