#+
# Copyright 2013 iXsystems, Inc.
# All rights reserved
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted providing that the following conditions
# are met:
# 1. Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
# 2. Redistributions in binary form must reproduce the above copyright
#    notice, this list of conditions and the following disclaimer in the
#    documentation and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE AUTHOR ``AS IS'' AND ANY EXPRESS OR
# IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
# WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED.  IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
# DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
# OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
# HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT,
# STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING
# IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.
#
#####################################################################
import logging

from django.shortcuts import render

from freenasUI.common.system import get_sw_name, get_sw_login_version
from freenasUI.support import utils

log = logging.getLogger("support.views")


def index(request):
    sw_name = get_sw_name().lower()
    return render(request, 'support/home_%s.html' % sw_name)


def ticket(request):
    if request.method == 'POST':
        success, msg = utils.new_ticket({
            'user': request.POST.get('username'),
            'password': request.POST.get('password'),
            'type': request.POST.get('type'),
            'title': request.POST.get('subject'),
            'body': request.POST.get('desc'),
            'version': get_sw_login_version(),
        })
        if not success:
            response = render(request, 'support/ticket.html', {
                'error_message': msg,
            })
        else:
            response = render(request, 'support/ticket_response.html', {
                'success': success,
                'message': msg,
            })
        if not request.is_ajax():
            response.content = (
                '<html><body><textarea>%s</textarea></boby></html>' % (
                    response.content,
                )
            )
        return response
    return render(request, 'support/ticket.html')
