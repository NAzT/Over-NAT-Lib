# coding: utf-8

import argparse
import asyncio
import logging
import socket
import sys
import threading
import time
import queue
import json

from os import path
#sys.path.append(path.dirname(path.abspath(__file__)) + "/../../")
#sys.path.insert(0, path.dirname(path.abspath(__file__)) + "/../../tmp/punch_sctp_plain_tmp/")
from aiortcdc import RTCPeerConnection, RTCSessionDescription

from signaling_share_ws import add_signaling_arguments, create_signaling

# application level ws communication
import websocket
import traceback

sctp_transport_established = False
force_exited = False

#channel_sender = None
remote_stdout_connected = False
remote_stdin_connected = False
sender_fifo_q = asyncio.Queue()
receiver_fifo_q = asyncio.Queue()
signaling = None
#clientsock = None
client_address = None
send_ws = None
sub_channel_sig = None
is_remote_node_exists_on_my_send_room = False

is_received_client_disconnect_request = False

server_send = None
server_rcv = None

async def consume_signaling(pc, signaling):
    global force_exited
    global remote_stdout_connected
    global remote_stin_connected

    while True:
        try:
            obj = await signaling.receive()

            if isinstance(obj, RTCSessionDescription):
                await pc.setRemoteDescription(obj)

                if obj.type == 'offer':
                    # send answer
                    await pc.setLocalDescription(await pc.createAnswer())
                    await signaling.send(pc.localDescription)
            elif isinstance(obj, str) and force_exited == False:
                #print("string recievd: " + obj, file=sys.stderr)
                continue
            else:
                print('Exiting', file=sys.stderr)
                break
        except:
            traceback.print_exc()


async def run_answer(pc, signaling):
    await signaling.connect()

    @pc.on('datachannel')
    def on_datachannel(channel):
        global sctp_transport_established
        start = time.time()
        octets = 0
        sctp_transport_established = True
        print("datachannel established")

        @channel.on('message')
        async def on_message(message):
            nonlocal octets
            global receiver_fifo_q
            #global clientsock

            try:
                print("message event fired", file=sys.stderr)
                print("message received from datachannel: " + str(len(message)), file=sys.stderr)
                if len(message) > 0:
                    octets += len(message)
                    await receiver_fifo_q.put(message)
                    # if clientsock != None:
                    #     clientsock.sendall(message)
                # else:
                #     elapsed = time.time() - start
                #     if elapsed == 0:
                #         elapsed = 0.001
                #     print('received %d bytes in %.1f s (%.3f Mbps)' % (
                #         octets, elapsed, octets * 8 / elapsed / 1000000), file=sys.stderr)

                # if clientsock != None:
                #     clientsock.close()
            except:
                traceback.print_exc()
                # if clientsock:
                #     clientsock.close()
                ws_sender_send_wrapper("receiver_disconnected")
                # say goodbye
                #await signaling.send(None)

    await signaling.send("join")
    await consume_signaling(pc, signaling)

async def run_offer(pc, signaling):
    while True:
        try:
            await signaling.connect()
            await signaling.send("joined_members")

            cur_num_str = await signaling.receive()
            #print("cur_num_str: " + cur_num_str, file=sys.stderr)
            if "ignoalable error" in cur_num_str:
                pass
            elif cur_num_str != "0":
                await asyncio.sleep(2)
                break

            #print("wait join of receiver", file=sys.stderr)
            await asyncio.sleep(1)
        except:
            traceback.print_exc()
    await signaling.connect()
    await signaling.send("join")

    channel_sender = pc.createDataChannel('filexfer')

    async def send_data_inner():
        nonlocal channel_sender
        global sctp_transport_established
        global sender_fifo_q
        global remote_stdout_connected
        #global clientsock

        # this line is needed?
        asyncio.set_event_loop(asyncio.new_event_loop())

        while True:
            sctp_transport_established = True
            while remote_stdout_connected == False:
                print("wait remote_std_connected", file=sys.stderr)
                await asyncio.sleep(1)

            print("start waiting buffer state is OK", file=sys.stderr)
            while channel_sender.bufferedAmount > channel_sender.bufferedAmountLowThreshold:
                #print("buffer info of channel: " + str(channel_sender.bufferedAmount) + " > " + str( channel_sender.bufferedAmountLowThreshold))
                await asyncio.sleep(1)

            print("start sending roop", file=sys.stderr)
            while channel_sender.bufferedAmount <= channel_sender.bufferedAmountLowThreshold:
                try:
                    data = None
                    try:
                        is_empty = sender_fifo_q.empty()
                        print("queue is empty? at send_data_inner: " + str(is_empty), file=sys.stderr)
                        if is_empty != True:
                            data = await sender_fifo_q.get()
                        else:
                            await asyncio.sleep(1)
                            continue
                        # print("try get data from queue")
                        # data = await sender_fifo_q.get()
                        # print("got data from queue")
                    except:
                        traceback.print_exc()

                    # data = fifo_q.getvalue()
                    if data:
                        #print(type(data))
                        if type(data) is str:
                            print("notify end of transfer")
                            #channel_sender.send(data)

                            #ws_sender_send_wrapper("sender_disconnected")
                            channel_sender.send(data.encode())
                        else:
                            print("send_data: " + str(len(data)))
                            channel_sender.send(data)

                    # if clientsock == None:
                    #     channel_sender.send(bytes("", encoding="utf-8"))
                    #     remote_stdout_connected = False
                    #     break

                    await asyncio.sleep(0.01)
                except:
                    traceback.print_exc()

    async def send_data():
        #send_data_inner_th = threading.Thread(target=send_data_inner)
        #send_data_inner_th.start()
        await send_data_inner()

    #channel_sender.on('bufferedamountlow', send_data)
    channel_sender.on('open', send_data)

    # send offer
    await pc.setLocalDescription(await pc.createOffer())
    await signaling.send(pc.localDescription)

    await consume_signaling(pc, signaling)

async def ice_establishment_state():
    global force_exited
    while(sctp_transport_established == False and "failed" not in pc.iceConnectionState):
        #print("ice_establishment_state: " + pc.iceConnectionState)
        await asyncio.sleep(1)
    if sctp_transport_established == False:
        print("hole punching to remote machine failed.", file=sys.stderr)
        force_exited = True
        try:
            loop.stop()
            loop.close()
        except:
            pass
        print("exit.")

# app level websocket sending should anytime use this (except join message)
def ws_sender_send_wrapper(msg):
    if send_ws:
        send_ws.send(sub_channel_sig + "_chsig:" + msg)

# app level websocket sending should anytime use this
def ws_sender_recv_wrapper():
    if send_ws:
        return send_ws.recv()
    else:
        return None

def work_as_parent():
    pass

async def sender_server_handler(reader, writer):
    global sender_fifo_q
    #global clientsock

    print('local server Waiting for connections...')

    byte_buf = b''

    try:
        #clientsock, client_address = server.accept()
        print("new client connected.")
        # wait remote server is connected with some program
        while remote_stdout_connected == False:
            print("wait remote_stdout_connected", file=sys.stderr)
            await asyncio.sleep(1)

        while True:
            rcvmsg = None
            try:
                rcvmsg = await reader.read(5120)
                byte_buf = b''.join([byte_buf, rcvmsg])
                print("received message from client", file=sys.stderr)
                print(len(rcvmsg), file=sys.stderr)

                # block sends until bufferd data amount is gleater than 100KB
                if(len(byte_buf) <= 1024 * 512) and (rcvmsg != None and len(rcvmsg) > 0): #1MB
                    print("current bufferd byteds: " + str(len(byte_buf)), file=sys.stderr)
                    await asyncio.sleep(0.01)
                    continue
            except:
                traceback.print_exc()
                # print("maybe client disconnect")
                # if clientsock:
                #     clientsock.close()
                #     clientsock = None
                # ws_sender_send_wrapper("sender_disconnected")

            #print("len of recvmsg:" + str(len(recvmsg)))
            if rcvmsg == None or len(rcvmsg) == 0:
                if len(byte_buf) > 0:
                    await sender_fifo_q.put(byte_buf)
                    testing_byte_buf = b''
                print("break")
                await sender_fifo_q.put(str("finished"))
                break
            else:
                #print("fifo_q.write(rcvmsg)")
                print("put bufferd bytes: " + str(len(byte_buf)), file=sys.stderr)
                await sender_fifo_q.put(byte_buf)
                #await sender_fifo_q.put(rcvmsg)
                byte_buf = b''
            await asyncio.sleep(0.01)
        #send_data()
    except:
        traceback.print_exc()
        # if clientsock:
        #         #     clientsock.close()
        #         #     clientsock = None
        # except Exception as e:
    #     print(e, file=sys.stderr)

async def sender_server():
    global server_send
    #asyncio.set_event_loop(asyncio.new_event_loop())

    #if not args.target:
    #    args.target = '0.0.0.0'
    # try:
    #     server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    #     server.bind(("127.0.0.1", 10100))
    #     server.listen()
    # except:
    #     traceback.print_exc()

    try:
        server_send = await asyncio.start_server(
            sender_server_handler, '127.0.0.1', 10100)
    except:
        traceback.print_exc()

    async with server_send:
        await server_send.serve_forever()

async def receiver_server_handler(reader, writer):
    global receiver_fifo_q
    #global clientsock, client_address
    global is_remote_node_exists_on_my_send_room
    global is_received_client_disconnect_request

    #if not args.target:
    #    args.target = '0.0.0.0'
    # server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # server.bind(("127.0.0.1", 10200))
    # server.listen()

    #print('Waiting for connections...', file=sys.stderr)
    while True:
        try:
            #clientsock, client_address = server.accept()
            #print("new client connected.", file=sys.stderr)
            # wait until remote node join to my send room

            # finish of handler function should disconnect connection between client
            # print("is_received_client_disconnect_request: " + str(is_received_client_disconnect_request))

            # if is_received_client_disconnect_request == True:
            #     is_received_client_disconnect_request = False
            #     await writer.write("finished".encode())
            #     return

            while is_remote_node_exists_on_my_send_room == False:
                ws_sender_send_wrapper("joined_members_sub")
                message = ws_sender_recv_wrapper()
                splited = message.split(":")
                member_num = int(splited[1])
                if member_num >= 2:
                    is_remote_node_exists_on_my_send_room = True
                else:
                    await asyncio.sleep(3)
                    #ws_sender_send_wrapper("receiver_connected")
                #time.sleep(3)


            ws_sender_send_wrapper("receiver_connected")

            data = None
            try:
                #print("try get data from queue")
                # done, pending = await asyncio.wait_for([receiver_fifo_q.get()], timeout=5)
                # tmp_loop = asyncio.get_event_loop()
                # data = loop.run_until_complete(done.pop().result())

                #data = await receiver_fifo_q.get()
                #qsize = await receiver_fifo_q.qdize()
                #if qsize > 0:
                is_empty = receiver_fifo_q.empty()
                print("queue is empty? at receiver_server_handler: " + str(is_empty), file=sys.stderr)
                if is_empty != True:
                    data = await receiver_fifo_q.get()
                else:
                    await asyncio.sleep(1)
                    continue
                #print("got get data from queue")
            except:
                traceback.print_exc()
                await asyncio.sleep(1)
                #pass

            # data = fifo_q.getvalue()
            if data:
                # if type(data) is "str" and data == "finished":
                #     print("notify end of transfer")
                #     # channel_sender.send(data)
                #     ws_sender_send_wrapper("sender_disconnected")
                # else:
                print("send_data: " + str(len(data)))
                writer.write(data)
                await writer.drain()
            await asyncio.sleep(0.01)
        except:
            traceback.print_exc()
            ws_sender_send_wrapper("receiver_disconnected")

async def receiver_server():
    global server_rcv
    try:
        server_rcv = await asyncio.start_server(
            receiver_server_handler, '127.0.0.1', 10200)
    except:
        traceback.print_exc()

    async with server_rcv:
        await server_rcv.serve_forever()
            #print(e, file=sys.stderr)

async def send_keep_alive():
    #logging.basicConfig(level=logging.FATAL)
    while True:
        ws_sender_send_wrapper("keepalive")
        #time.sleep(5)
        await asyncio.sleep(5)

def setup_ws_sub_sender():
    global send_ws
    global sub_channel_sig
    send_ws = websocket.create_connection(ws_protcol_str +  "://" + args.signaling_host + ":" + str(args.signaling_port) + "/")
    print("sender app level ws opend")
    if args.role == 'send':
        sub_channel_sig = args.gid + "stor"
    else:
        sub_channel_sig = args.gid + "rtos"
    ws_sender_send_wrapper("join")

    # ws_keep_alive_th = threading.Thread(target=send_keep_alive)
    # ws_keep_alive_th.start()

def ws_sub_receiver():
    def on_message(ws, message):
        global remote_stdout_connected
        global remote_stdin_connected
        global done_reading
        #global clientsock
        global is_remote_node_exists_on_my_send_room
        global is_received_client_disconnect_request

        #print(message,  file=sys.stderr)
        print("called on_message", file=sys.stderr)
        #print(message)

        if "receiver_connected" in message:
            if remote_stdout_connected == False:
                print("receiver_connected")
            #print(fifo_q.getbuffer().nbytes)
            remote_stdout_connected = True
            # if fifo_q.getbuffer().nbytes != 0:
            #     send_data()
        elif "receiver_disconnected" in message:
            remote_stdout_connected = False
            done_reading = False
        elif "sender_connected" in message:
            remote_stdin_connected = True
        elif "sender_disconnected" in message:
            print("sender_disconnected")
            remote_stdin_connected = False
            is_received_client_disconnect_request = True
            # if clientsock:
            #     time.sleep(5)
            #     print("disconnect clientsock")
            #     clientsock.close()
            #     clientsock = None

    def on_error(ws, error):
        print(error)

    def on_close(ws):
        print("### closed ###")

    def on_open(ws):
        print("receiver app level ws opend")
        try:
            if args.role == 'send':
                ws.send(args.gid + "rtos_chsig:join")
            else:
                ws.send(args.gid + "stor_chsig:join")
        except:
            traceback.print_exc()

    #logging.basicConfig(level=logging.DEBUG)
    #websocket.enableTrace(True)
    ws = websocket.WebSocketApp(ws_protcol_str + "://" + args.signaling_host + ":" + str(args.signaling_port) + "/",
                                    on_message=on_message,
                                    on_error=on_error,
                                    on_close=on_close)
    ws.on_open = on_open
    ws.run_forever()

async def parallel_by_gather():
    # execute by parallel
    def notify(order):
        print(order + " has just finished.")

    cors = None
    if args.role == 'send':
        cors = [run_offer(pc, signaling), sender_server(), ice_establishment_state(), send_keep_alive()]
    else:
        cors = [run_answer(pc, signaling), receiver_server(), ice_establishment_state(), send_keep_alive()]
    await asyncio.gather(*cors)
    return

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Data channel file transfer')
    parser.add_argument('hierarchy', choices=['parent', 'child'])
    parser.add_argument('gid')
    #parser.add_argument('filename')
    parser.add_argument('--role', choices=['send', 'receive'])
    parser.add_argument('--verbose', '-v', action='count')
    parser.add_argument('--send-stream-port', default=10100,
                        help='This local server make datachannel stream readable at this port')
    parser.add_argument('--recv-stream-port', default=10200,
                        help='This local server make datachannel stream readable at this port')
    add_signaling_arguments(parser)
    args = parser.parse_args()

    colo = None
    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    #logging.basicConfig(level=logging.FATAL)

    ws_protcol_str = "ws"
    if args.secure_signaling == True:
        ws_protcol_str = "wss"

    if args.hierarchy == 'parent':
        colo = work_as_parent()
    else:
        signaling = create_signaling(args)
        pc = RTCPeerConnection()


        #
        # ice_state_th = threading.Thread(target=ice_establishment_state)
        # ice_state_th.start()
        #
        setup_ws_sub_sender()

        # this feature inner syori is nazo, so not use event loop
        ws_sub_recv_th = threading.Thread(target=ws_sub_receiver)
        ws_sub_recv_th.start()

        #ws_sub_recv_loop = asyncio.new_event_loop()
        #ws_sub_recv_loop.run(ws_sub_receiver())
        #print("after ws_sub_recv_loop.run")

        # if args.role == 'send':
        #     #fp = open(args.filename, 'rb')
        #     sender_th = threading.Thread(target=sender_server)
        #     sender_th.start()
        #     coro = run_offer(pc, signaling)
        # else:
        #     #fp = open(args.filename, 'wb')
        #     receiver_th = threading.Thread(target=receiver_server)
        #     receiver_th.start()
        #     coro = run_answer(pc, signaling)

    loop = None
    try:
        # run event loop
        loop = asyncio.get_event_loop()
        try:
            #loop.run_until_complete(coro)
            loop.run_until_complete(parallel_by_gather())
        except:
            traceback.print_exc()
        finally:
            #fp.close()
            loop.run_until_complete(pc.close())
            loop.run_until_complete(signaling.close())
    except:
        traceback.print_exc()
